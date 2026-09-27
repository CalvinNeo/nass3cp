"""Browser uploads with one staged file and the verified NAS upload pipeline."""

import posixpath
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Mapping, Optional

from .client import ApiClient, upload
from .errors import Nass3cpError, UploadCancelled


MAX_FILES = 1000
TERMINAL = ("complete", "cancelled", "failed")


class UploadError(Nass3cpError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _Item:
    def __init__(self, folder: str, name: str, size: int, mtime_ms: int):
        self.id = secrets.token_hex(16)
        self.folder, self.name = folder, name
        self.path = posixpath.join(folder, name)
        self.size, self.mtime_ms = size, mtime_ms
        self.status, self.phase = "waiting", "Waiting for local file"
        self.received = self.sent = 0
        self.error: Optional[str] = None
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.cancelled = threading.Event()
        self.transfer_id: Optional[str] = None
        self.abort_started = False
        self.directory: Any = None


class _TrackedApi:
    def __init__(self, api: ApiClient, item: _Item, lock: Any):
        self.api, self.item, self.lock = api, item, lock

    def __getattr__(self, name: str) -> Any:
        return getattr(self.api, name)

    def create_upload(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if self.item.cancelled.is_set():
            raise UploadCancelled("Upload cancelled")
        value = self.api.create_upload(*args, **kwargs)
        with self.lock:
            self.item.transfer_id = value.get("id")
        if self.item.cancelled.is_set():
            self.api.abort(self.item.transfer_id)
            raise UploadCancelled("Upload cancelled")
        return value


class UploadManager:
    def __init__(self, api_factory: Callable[[], ApiClient], jobs: int = 2,
                 inflight: int = 3, transfer_timeout: int = 86400):
        self.api_factory = api_factory
        self.jobs, self.inflight, self.transfer_timeout = jobs, inflight, transfer_timeout
        self._condition = threading.Condition(threading.RLock())
        self._items = OrderedDict()  # type: OrderedDict
        self._active: Optional[_Item] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._closed = False

    def state(self) -> Dict[str, Any]:
        with self._condition:
            items = []
            for item in self._items.values():
                elapsed = (item.finished or time.monotonic()) - item.started if item.started else 0
                items.append({
                    "id": item.id, "path": item.path, "name": item.name, "size": item.size,
                    "mtime_ms": item.mtime_ms, "status": item.status, "phase": item.phase,
                    "received": item.received, "sent": item.sent, "error": item.error,
                    "bytes_per_second": int(item.sent / elapsed) if elapsed > 0 else 0,
                })
            return {"items": items, "concurrency": 1, "busy": self._active is not None}

    def enqueue(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        folder, files = body.get("path"), body.get("files")
        if (not isinstance(folder, str) or not folder or "\x00" in folder
                or len(folder.encode("utf-8", "surrogatepass")) > 8192):
            raise UploadError(400, "Invalid NAS folder path.")
        if body.get("overwrite", False) is not False:
            raise UploadError(400, "Browser uploads do not replace existing files.")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise UploadError(400, "Select between 1 and %d files." % MAX_FILES)
        folder = posixpath.normpath(folder)
        candidates = []
        names = set()
        for record in files:
            if not isinstance(record, dict):
                raise UploadError(400, "Invalid file metadata.")
            name, size, mtime = record.get("name"), record.get("size"), record.get("mtime_ms", 0)
            if (not isinstance(name, str) or not name or name in (".", "..")
                    or re.search(r"[/\\\x00-\x1f\x7f\ud800-\udfff]", name)
                    or len(name.encode("utf-8")) > 255):
                raise UploadError(400, "Invalid filename. Select files, not folders or paths.")
            if (isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 2**53 - 1
                    or isinstance(mtime, bool) or not isinstance(mtime, int)
                    or not 0 <= mtime <= 9_223_372_036_854):
                raise UploadError(400, "Invalid file size or modification time.")
            if name in names:
                raise UploadError(400, "Select only one file with each name.")
            names.add(name)
            candidates.append(_Item(folder, name, size, mtime))
        with self._condition:
            if self._closed:
                raise UploadError(503, "The local browser service is closing.")
            active = {item.path: item for item in self._items.values() if item.status not in TERMINAL}
            selected = []
            for item in candidates:
                existing = active.get(item.path)
                if existing is not None:
                    if (existing.status != "waiting" or existing.size != item.size
                            or existing.mtime_ms != item.mtime_ms):
                        raise UploadError(409, "An upload to this path is already pending: " + item.path)
                    item = existing
                selected.append(item)
            added = sum(item.id not in self._items for item in selected)
            for identifier, item in list(self._items.items()):
                if item.status in TERMINAL and len(self._items) + added > MAX_FILES:
                    del self._items[identifier]
            if len(self._items) + added > MAX_FILES:
                raise UploadError(409, "The upload queue is full. Finish or cancel pending files.")
            for item in selected:
                self._items[item.id] = item
            value = self.state()
            value["enqueued_ids"] = [item.id for item in selected]
            return value

    def _item(self, identifier: str) -> _Item:
        item = self._items.get(identifier)
        if item is None:
            raise UploadError(404, "Upload does not belong to this browser session.")
        return item

    @staticmethod
    def _check_cancelled(item: _Item) -> None:
        if item.cancelled.is_set():
            raise UploadCancelled("Upload cancelled")

    def _finish(self, item: _Item, status: str, phase: str, error: Optional[str] = None) -> None:
        with self._condition:
            item.status, item.phase, item.error = status, phase, error
            item.finished = time.monotonic()
            self._active = None
            self._condition.notify_all()

    def receive(self, identifier: str, stream: BinaryIO, length: int) -> Dict[str, Any]:
        with self._condition:
            item = self._item(identifier)
            if self._closed:
                raise UploadError(503, "The local browser service is closing.")
            if item.status != "waiting":
                raise UploadError(409, "This upload is no longer waiting for a file.")
            if length != item.size:
                raise UploadError(400, "File length does not match the queued file.")
            if self._active is not None:
                raise UploadError(409, "Another file is being uploaded. Wait for it to finish.")
            self._active = item
            item.status, item.phase = "receiving", "Receiving local file"
        directory = None
        handed_off = False
        failure = "Local file was not received. Select it again to retry."
        try:
            api = self.api_factory()
            info = api.path_info(item.folder)
            if not info.get("exists") or info.get("type") != "directory":
                raise UploadError(400, "The upload destination must be an existing NAS folder.")
            if api.path_info(item.path).get("exists"):
                raise UploadError(409, "A file or folder with this name already exists on the NAS.")
            self._check_cancelled(item)
            directory = tempfile.TemporaryDirectory(prefix="nass3cp-browser-upload-")
            if shutil.disk_usage(directory.name).free < item.size:
                raise UploadError(507, "Not enough local temporary disk space for this file.")
            # A NAS filename is never used as a local staging path.
            target = Path(directory.name) / "payload.nass3cp-part"
            read = getattr(stream, "read1", stream.read)
            with target.open("wb") as handle:
                remaining = item.size
                while remaining:
                    self._check_cancelled(item)
                    block = read(min(1024 * 1024, remaining))
                    if not block:
                        raise UploadError(400, "Local file transfer was interrupted. Select the file again.")
                    handle.write(block)
                    remaining -= len(block)
                    with self._condition:
                        item.received = item.size - remaining
            with self._condition:
                self._check_cancelled(item)
                item.directory = directory
                item.status, item.phase = "queued", "Queued for NAS"
                if self._worker_thread is None:
                    self._worker_thread = threading.Thread(target=self._worker, daemon=True)
                    self._worker_thread.start()
                handed_off = True
                self._condition.notify_all()
            return self.state()
        except Exception as exc:
            failure = str(exc)
            if item.cancelled.is_set():
                raise UploadError(409, "Upload cancelled.") from exc
            raise
        finally:
            if not handed_off:
                try:
                    if directory is not None:
                        directory.cleanup()
                finally:
                    if item.cancelled.is_set():
                        self._finish(item, "cancelled", "Cancelled")
                    else:
                        self._finish(item, "failed", "Failed", failure)

    def _worker(self) -> None:
        while True:
            with self._condition:
                while self._active is None or self._active.directory is None:
                    if self._closed:
                        return
                    self._condition.wait()
                item = self._active
            self._transfer(item)

    def _transfer(self, item: _Item) -> None:
        status, phase, error = "complete", "Uploaded to NAS", None
        api = None
        try:
            self._check_cancelled(item)
            api = _TrackedApi(self.api_factory(), item, self._condition)
            with self._condition:
                item.started = time.monotonic()

            def progress(label: str, completed: int, total: int) -> None:
                self._check_cancelled(item)
                with self._condition:
                    if item.status == "cancelling":
                        return
                    if label == "upload to S3":
                        item.status, item.phase = "uploading", "Uploading to NAS"
                        item.sent = max(0, min(completed, item.size))
                    else:
                        item.status, item.phase = "verifying", "Verifying and saving on NAS"

            upload(api, str(Path(item.directory.name) / "payload.nass3cp-part"), item.path,
                   False, self.jobs, self.transfer_timeout, True, self.inflight,
                   destination_mtime_ns=item.mtime_ms * 1_000_000,
                   progress_callback=progress, cancel_event=item.cancelled)
            item.sent = item.size
        except Exception as exc:
            if item.cancelled.is_set():
                status, phase = "cancelled", "Cancelled"
            else:
                status, phase, error = "failed", "Failed", str(exc)
        finally:
            if api is not None and item.transfer_id and status != "complete":
                self._abort(item.transfer_id)
            try:
                item.directory.cleanup()
            except OSError:
                error = "Could not remove the local temporary upload file."
            finally:
                item.directory = None
                self._finish(item, status, phase, error)

    def _abort(self, identifier: str) -> None:
        try:
            self.api_factory().abort(identifier)
        except Exception:
            pass  # NAS expiry and bucket lifecycle rules also cover disconnection.

    def cancel(self, identifier: str) -> Dict[str, Any]:
        with self._condition:
            item = self._item(identifier)
            if item.status not in TERMINAL:
                item.cancelled.set()
                if item.status == "waiting":
                    item.status, item.phase = "cancelled", "Cancelled"
                    item.finished = time.monotonic()
                else:
                    item.status, item.phase = "cancelling", "Cancelling…"
                if item.transfer_id and not item.abort_started:
                    item.abort_started = True
                    threading.Thread(target=self._abort, args=(item.transfer_id,), daemon=True).start()
                self._condition.notify_all()
            return self.state()

    def stop(self) -> None:
        with self._condition:
            self._closed = True
            for identifier in self._items:
                self.cancel(identifier)
            self._condition.notify_all()
            while self._active is not None:
                self._condition.wait(timeout=0.5)
        if self._worker_thread is not None:
            self._worker_thread.join()
