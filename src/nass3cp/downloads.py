"""Per-file browser download queue using the existing verified S3 pipeline."""

import posixpath
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .client import ApiClient, download
from .errors import DownloadCancelled, Nass3cpError


MAX_FILES = 1000
TERMINAL = ("complete", "cancelled", "failed", "expired")


class DownloadError(Nass3cpError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def safe_filename(path: str) -> str:
    name = posixpath.basename(path)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f\ud800-\udfff]', "_", name).rstrip(" .")
    if not name or name in (".", ".."):
        name = "download"
    if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(
            "%s%d" % (prefix, index) for prefix in ("COM", "LPT") for index in range(1, 10))}:
        name = "_" + name
    if len(name.encode("utf-8")) > 240:
        stem, suffix = posixpath.splitext(name)
        suffix = suffix[:20]
        while len((stem + suffix).encode("utf-8")) > 240:
            stem = stem[:-1]
        name = stem + suffix
    return name


class _Item:
    def __init__(self, path: str, transport: str = "s3"):
        self.id = secrets.token_hex(16)
        self.path, self.name = path, safe_filename(path)
        self.transport = transport
        self.status, self.phase = "queued", "Queued"
        self.total: Optional[int] = None
        self.received = self.sent = 0
        self.created = time.monotonic()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.error: Optional[str] = None
        self.cancelled = threading.Event()
        self.artifact: Optional[Path] = None
        self.streaming = False
        self.transfer_id: Optional[str] = None
        self.abort_started = False


class _TrackedApi:
    def __init__(self, api: ApiClient, item: _Item, lock: Any):
        self.api, self.item, self.lock = api, item, lock

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.api, name)
        if name == "abort" or not callable(method):
            return method

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.item.cancelled.is_set():
                raise DownloadCancelled("Download cancelled")
            value = method(*args, **kwargs)
            if name == "create_download":
                with self.lock:
                    self.item.transfer_id = value.get("id")
            if self.item.cancelled.is_set():
                raise DownloadCancelled("Download cancelled")
            return value

        return call


class DownloadManager:
    def __init__(self, api_factory: Callable[[], ApiClient], concurrency: int = 1,
                 jobs: int = 2, inflight: int = 3, transfer_timeout: int = 86400,
                 ready_timeout: float = 600, direct_transfers: Any = None,
                 transports: Any = ("s3",)):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 8:
            raise ValueError("download concurrency must be between 1 and 8")
        self.api_factory, self.concurrency = api_factory, concurrency
        self.jobs, self.inflight, self.transfer_timeout = jobs, inflight, transfer_timeout
        self.ready_timeout = ready_timeout
        self.direct_transfers, self.transports = direct_transfers, transports
        self._condition = threading.Condition(threading.RLock())
        self._items = OrderedDict()  # type: OrderedDict
        self._queue = deque()  # type: deque
        self._threads = []  # type: list
        self._closed = False

    def _snapshot(self, item: _Item) -> Dict[str, Any]:
        elapsed = time.monotonic() - item.started if item.started is not None else 0
        return {
            "id": item.id, "path": item.path, "name": item.name, "status": item.status,
            "phase": item.phase, "size": item.total, "received": item.received,
            "sent": item.sent, "error": item.error,
            "transport": item.transport,
            "bytes_per_second": int(item.received / elapsed) if elapsed > 0 else 0,
        }

    def state(self) -> Dict[str, Any]:
        with self._condition:
            return {"concurrency": self.concurrency,
                    "items": [self._snapshot(item) for item in self._items.values()]}

    def enqueue(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        transport = body.get("transport", self.transports[0])
        if transport not in self.transports or transport == "nathole" and self.direct_transfers is None:
            raise DownloadError(400, "The selected transfer method is not available.")
        paths = body.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
            raise DownloadError(400, "Select between 1 and %d files." % MAX_FILES)
        for path in paths:
            if (not isinstance(path, str) or not path or "\x00" in path
                    or len(path.encode("utf-8", "surrogatepass")) > 8192 or path.endswith("/")):
                raise DownloadError(400, "Invalid NAS file path.")
        with self._condition:
            if self._closed:
                raise DownloadError(503, "The local browser service is closing.")
            existing = {item.path for item in self._items.values() if item.status not in TERMINAL}
            paths = list(dict.fromkeys(path for path in paths if path not in existing))
            for identifier, item in list(self._items.items()):
                if item.status in TERMINAL and (len(self._items) + len(paths) > MAX_FILES
                        or item.finished is not None and time.monotonic() - item.finished > 3600):
                    del self._items[identifier]
            if len(self._items) + len(paths) > MAX_FILES:
                raise DownloadError(409, "The download queue is full. Wait for files to finish.")
            enqueued = []
            for path in paths:
                item = _Item(path, transport)
                self._items[item.id] = item
                self._queue.append(item)
                enqueued.append(item.id)
            if not self._threads:
                for _ in range(self.concurrency):
                    worker = threading.Thread(target=self._worker, daemon=True)
                    self._threads.append(worker)
                    worker.start()
            self._condition.notify_all()
            value = self.state()
            value["enqueued_ids"] = enqueued
            return value

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                item = self._queue.popleft()
                if item.status != "queued":
                    continue
                item.status, item.phase = "preparing", "Checking file"
            self._prepare(item)

    def _prepare(self, item: _Item) -> None:
        api = None
        try:
            api = _TrackedApi(self.api_factory(), item, self._condition)
            info = api.path_info(item.path)
            if not info.get("exists") or info.get("type") != "file":
                raise DownloadError(400, "This UI downloads regular files only; folders are not supported.")
            with self._condition:
                item.total = info["size"]
                item.started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="nass3cp-browser-download-") as directory:
                if shutil.disk_usage(directory).free < item.total:
                    raise Nass3cpError("Not enough local temporary disk space for this file.")
                # Even a verified cache remains explicitly temporary until the
                # browser has received it. No NAS filename becomes a local path.
                target = Path(directory) / "payload.nass3cp-part"

                def progress(label: str, completed: int, total: int) -> None:
                    if item.cancelled.is_set():
                        raise DownloadCancelled("Download cancelled")
                    with self._condition:
                        if item.status == "cancelling":
                            return
                        if label != "waiting for nathole":
                            item.total = total
                        if label == "verify download":
                            item.status, item.phase = "verifying", "Verifying SHA-256"
                        elif label in ("download from S3", "copy from NAS via S3", "download via nathole"):
                            item.status, item.phase = "downloading", "Downloading"
                            item.received = max(0, min(completed, total))
                        elif label == "waiting for nathole":
                            item.status, item.phase = "preparing", "Waiting for nathole"
                        else:
                            item.status, item.phase = "preparing", "Preparing on NAS"

                try:
                    if item.transport == "nathole":
                        self.direct_transfers.download(item.path, str(target), progress, item.cancelled)
                    else:
                        download(api, item.path, str(target), False, self.jobs, self.transfer_timeout,
                                 True, self.inflight, progress_callback=progress, cancel_event=item.cancelled)
                finally:
                    if item.transfer_id is not None:
                        if item.cancelled.is_set() or not target.exists():
                            api.abort(item.transfer_id)
                        with self._condition:
                            item.transfer_id = None
                with self._condition:
                    if item.cancelled.is_set():
                        raise DownloadCancelled("Download cancelled")
                    item.total = item.received = target.stat().st_size
                    item.artifact = target
                    item.status, item.phase = "ready", "Ready to save"
                    deadline = time.monotonic() + self.ready_timeout
                    self._condition.notify_all()
                    # Keep the concurrency slot until the browser receives this
                    # file, so blocked downloads cannot accumulate a disk cache.
                    while item.status != "complete":
                        if not item.streaming:
                            if item.cancelled.is_set():
                                raise DownloadCancelled("Download cancelled")
                            if time.monotonic() >= deadline:
                                item.status, item.phase = "expired", "Save link expired"
                                item.error = "The file was not saved within 10 minutes. Queue it again."
                                break
                        self._condition.wait(timeout=0.5)
                    item.artifact = None
        except DownloadCancelled:
            with self._condition:
                item.status, item.phase = "cancelled", "Cancelled"
                item.error = None
        except Exception as exc:
            with self._condition:
                if item.cancelled.is_set():
                    item.status, item.phase, item.error = "cancelled", "Cancelled", None
                else:
                    item.status, item.phase, item.error = "failed", "Failed", str(exc)
        finally:
            with self._condition:
                item.artifact = None
                item.finished = time.monotonic()
                self._condition.notify_all()

    def _abort(self, identifier: str) -> None:
        try:
            self.api_factory().abort(identifier)
        except Exception:
            pass  # The transfer timeout and NAS cleanup also cover disconnection.

    def cancel(self, identifier: str) -> Dict[str, Any]:
        with self._condition:
            item = self._items.get(identifier)
            if item is None:
                raise DownloadError(404, "Download does not belong to this browser session.")
            if item.status not in TERMINAL:
                item.cancelled.set()
                if item.status == "queued":
                    self._queue.remove(item)
                    item.status, item.phase = "cancelled", "Cancelled"
                    item.finished = time.monotonic()
                else:
                    item.status, item.phase = "cancelling", "Cancelling…"
                if item.transfer_id and not item.abort_started:
                    item.abort_started = True
                    threading.Thread(target=self._abort, args=(item.transfer_id,), daemon=True).start()
                self._condition.notify_all()
            return self.state()

    def open_file(self, identifier: str) -> Any:
        with self._condition:
            item = self._items.get(identifier)
            if item is None:
                raise DownloadError(404, "Download does not belong to this browser session.")
            if item.status != "ready" or item.artifact is None:
                raise DownloadError(409, "This file is not ready to save. Check the download table.")
            handle = item.artifact.open("rb")
            item.status, item.phase = "sending", "Sending to browser"
            item.streaming = True
            item.sent, item.error = 0, None
            return item, handle

    def sent_bytes(self, item: _Item, count: int) -> None:
        if item.cancelled.is_set():
            raise DownloadCancelled("Download cancelled")
        with self._condition:
            item.sent = count

    def finish_sending(self, item: _Item, complete: bool) -> None:
        with self._condition:
            item.streaming = False
            if complete:
                item.status, item.phase = "complete", "Sent to browser"
                item.finished = time.monotonic()
            elif not item.cancelled.is_set():
                item.status, item.phase = "ready", "Ready to save"
                item.error = "Browser connection interrupted. Click Save file to try again."
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._closed = True
            for identifier in list(self._items):
                self.cancel(identifier)
            self._condition.notify_all()
        for worker in self._threads:
            # Network operations have timeouts. Wait for cooperative cancellation
            # so normal CLI shutdown does not abandon open temporary files.
            worker.join()
