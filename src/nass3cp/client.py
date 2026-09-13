import hashlib
import json
import os
import ssl
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .errors import Nass3cpError, ProtocolError
from .net import secure_opener


_DATA_OPENERS = threading.local()
_PROGRESS_REPORT_BYTES = 256 * 1024


def _human_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = max(0.0, float(value))
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return "%d B" % int(amount)
            return "%.1f %s" % (amount, unit)
        amount /= 1024.0
    return "%.1f PiB" % amount


def _human_duration(seconds: float) -> str:
    value = max(0, int(seconds + 0.5))
    if value >= 3600:
        return "%d:%02d:%02d" % (value // 3600, value // 60 % 60, value % 60)
    if value >= 60:
        return "%d:%02d" % (value // 60, value % 60)
    return "%ds" % value


class _ProgressDisplay:
    def __init__(self, enabled: bool, stream: Optional[TextIO] = None):
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr
        try:
            self.is_terminal = bool(self.stream.isatty())
        except (AttributeError, OSError):
            self.is_terminal = False
        self._lock = threading.Lock()
        self._label: Optional[str] = None
        self._started_at = 0.0
        self._started_bytes = 0
        self._last_rendered_at = 0.0
        self._last_rendered_bytes: Optional[int] = None
        self._last_width = 0
        self._line_open = False

    @staticmethod
    def _bar(percent: float, width: int = 24) -> str:
        complete = min(width, int(percent * width / 100.0))
        if complete >= width:
            return "=" * width
        return "=" * complete + ">" + "." * (width - complete - 1)

    def _line(self, label: str, completed: int, total: int, now: float) -> str:
        percent = 100.0 if total == 0 else min(100.0, completed * 100.0 / total)
        line = "%s: [%s] %5.1f%%  %s/%s" % (
            label,
            self._bar(percent),
            percent,
            _human_bytes(completed),
            _human_bytes(total),
        )
        elapsed = now - self._started_at
        progressed = completed - self._started_bytes
        if elapsed >= 0.5 and progressed > 0:
            rate = progressed / elapsed
            line += "  %s/s" % _human_bytes(rate)
            if completed < total:
                line += "  ETA %s" % _human_duration((total - completed) / rate)
        return line

    def update(self, label: str, completed: int, total: int, force: bool = False) -> None:
        if not self.enabled:
            return
        total = max(0, int(total))
        completed = max(0, min(int(completed), total))
        now = time.monotonic()
        with self._lock:
            changed_phase = label != self._label
            if changed_phase:
                if self.is_terminal and self._line_open:
                    self.stream.write("\n")
                self._label = label
                self._started_at = now
                self._started_bytes = completed
                self._last_rendered_at = 0.0
                self._last_rendered_bytes = None
                self._last_width = 0
                self._line_open = False
                force = True

            finished = total == 0 or completed >= total
            if finished and completed == self._last_rendered_bytes:
                return
            interval = 0.1 if self.is_terminal else 1.0
            if not force and now - self._last_rendered_at < interval:
                return
            if not self.is_terminal and not force and completed == self._last_rendered_bytes:
                return

            line = self._line(label, completed, total, now)
            if self.is_terminal:
                padding = " " * max(0, self._last_width - len(line))
                self.stream.write("\r" + line + padding)
                self._last_width = len(line)
                self._line_open = True
            else:
                self.stream.write(line + "\n")
            self.stream.flush()
            self._last_rendered_at = now
            self._last_rendered_bytes = completed

    def close(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self.is_terminal and self._line_open:
                self.stream.write("\n")
                self.stream.flush()
                self._line_open = False


class _BatchProgress:
    def __init__(
        self,
        display: _ProgressDisplay,
        label: str,
        completed_before_batch: int,
        total: int,
        sizes: Sequence[int],
    ):
        self.display = display
        self.label = label
        self.completed_before_batch = completed_before_batch
        self.total = total
        self.sizes = list(sizes)
        self.current = [0] * len(self.sizes)
        self._lock = threading.Lock()

    def callback(self, index: int) -> Callable[[int], None]:
        def report(value: int) -> None:
            with self._lock:
                self.current[index] = max(0, min(int(value), self.sizes[index]))
                completed = self.completed_before_batch + sum(self.current)
            self.display.update(self.label, completed, self.total)

        return report


class _ProgressReader:
    def __init__(self, data: bytes, report: Callable[[int], None]):
        self._data = data
        self._report = report
        self._position = 0
        self._last_reported = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            end = len(self._data)
        else:
            end = min(len(self._data), self._position + size)
        value = self._data[self._position : end]
        self._position = end
        if (
            self._position == len(self._data)
            or self._position - self._last_reported >= _PROGRESS_REPORT_BYTES
        ):
            self._report(self._position)
            self._last_reported = self._position
        return value


def _data_opener():
    opener = getattr(_DATA_OPENERS, "opener", None)
    if opener is None:
        opener = secure_opener()
        _DATA_OPENERS.opener = opener
    return opener


class ApiClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        ca_file: Optional[str] = None,
        insecure: bool = False,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ProtocolError("NAS service URL must use HTTP or HTTPS")
        if parsed.scheme == "http" and (ca_file is not None or insecure):
            raise ProtocolError("TLS options cannot be used with an HTTP NAS service")
        self.password = password
        self.timeout = timeout
        if insecure:
            self.context = ssl._create_unverified_context()  # nosec - explicit CLI opt-in
        else:
            self.context = ssl.create_default_context(cafile=ca_file)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = secure_opener(self.context)

    def request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        data = None
        headers = {
            "Authorization": "Bearer " + self.password,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=self.timeout)
            try:
                raw = response.read(2 * 1024 * 1024 + 1)
            finally:
                response.close()
            if len(raw) > 2 * 1024 * 1024:
                raise ProtocolError("server response is too large")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ProtocolError("server returned a non-object JSON response")
            return value
        except HTTPError as exc:
            try:
                value = json.loads(exc.read(65536).decode("utf-8"))
                message = value["error"]["message"]
            except Exception:
                message = "HTTP %d" % exc.code
            raise ProtocolError("server request failed: %s" % message) from exc
        except (URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            raise ProtocolError("cannot reach NAS service: %s" % reason) from exc
        except (UnicodeDecodeError, ValueError, KeyError) as exc:
            raise ProtocolError("server returned invalid JSON") from exc

    def create_upload(self, path: str, size: int, mtime_ns: int, overwrite: bool) -> Dict[str, Any]:
        return self.request(
            "POST",
            "/v1/transfers/upload",
            {"path": path, "size": size, "mtime_ns": mtime_ns, "overwrite": overwrite},
        )

    def create_download(self, path: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/transfers/download", {"path": path})

    def state(self, transfer_id: str) -> Dict[str, Any]:
        return self.request("GET", "/v1/transfers/%s" % transfer_id)

    def urls(self, transfer_id: str, start: int, count: int) -> List[Dict[str, Any]]:
        value = self.request(
            "GET", "/v1/transfers/%s/urls?start=%d&count=%d" % (transfer_id, start, count)
        )
        items = value.get("items")
        if not isinstance(items, list):
            raise ProtocolError("server URL response has no items array")
        return items

    def commit(self, transfer_id: str, digest: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/transfers/%s/commit" % transfer_id, {"sha256": digest})

    def acknowledge(self, transfer_id: str) -> Dict[str, Any]:
        return self.request("POST", "/v1/transfers/%s/ack" % transfer_id, {})

    def abort(self, transfer_id: str) -> None:
        try:
            self.request("POST", "/v1/transfers/%s/abort" % transfer_id, {})
        except Nass3cpError:
            pass


def _data_request(
    method: str,
    item: Mapping[str, Any],
    data: Optional[bytes] = None,
    expected: Optional[int] = None,
    attempts: int = 4,
    progress: Optional[Callable[[int], None]] = None,
) -> bytes:
    url = item.get("url")
    headers = item.get("headers", {})
    if not isinstance(url, str) or not isinstance(headers, dict):
        raise ProtocolError("server returned an invalid or non-HTTPS S3 URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProtocolError("server returned an invalid or non-HTTPS S3 URL")
    clean_headers: Dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ProtocolError("server returned invalid S3 request headers")
        try:
            name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ProtocolError("server returned unsafe S3 request headers") from exc
        lowered = name.lower()
        if (
            not name.replace("-", "").isalnum()
            or lowered not in ("content-length", "content-type")
            and not lowered.startswith(("x-amz-", "x-oss-"))
            or "\r" in value
            or "\n" in value
        ):
            raise ProtocolError("server returned unsafe S3 request headers")
        clean_headers[name] = value
    signed_length = next(
        (value for name, value in clean_headers.items() if name.lower() == "content-length"),
        None,
    )
    if method == "PUT" and signed_length != str(len(data or b"")):
        raise ProtocolError("server returned an invalid signed S3 content length")

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        if progress is not None:
            progress(0)
        request_data: Any = data
        if method == "PUT" and progress is not None:
            request_data = _ProgressReader(data or b"", progress)
        request = Request(url, data=request_data, headers=clean_headers, method=method)
        try:
            response = _data_opener().open(request, timeout=300)
            try:
                if method == "GET":
                    parts = []
                    downloaded = 0
                    while True:
                        read_size = _PROGRESS_REPORT_BYTES
                        if expected is not None:
                            remaining = expected + 1 - downloaded
                            if remaining <= 0:
                                break
                            read_size = min(read_size, remaining)
                        part = response.read(read_size)
                        if not part:
                            break
                        parts.append(part)
                        downloaded += len(part)
                        if progress is not None:
                            progress(downloaded)
                    result = b"".join(parts)
                else:
                    response.read()
                    result = b""
            finally:
                response.close()
            if expected is not None and len(result) != expected:
                raise ProtocolError(
                    "S3 chunk has size %d, expected %d" % (len(result), expected)
                )
            if progress is not None:
                progress(len(data or b"") if method == "PUT" else len(result))
            return result
        except HTTPError as exc:
            try:
                detail = exc.read(4096).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            last_error = ProtocolError(
                "S3 %s failed with HTTP %d%s"
                % (method, exc.code, (": " + detail) if detail else "")
            )
        except (URLError, OSError, HTTPException, ProtocolError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(8.0, 0.5 * (2 ** attempt)))
    raise ProtocolError("S3 %s failed after %d attempts: %s" % (method, attempts, last_error))


def _verify_state(state: Mapping[str, Any], direction: str) -> Tuple[str, int, int, int]:
    transfer_id = state.get("id")
    size = state.get("size")
    chunks = state.get("chunks")
    chunk_size = state.get("chunk_size")
    if state.get("direction") != direction:
        raise ProtocolError("server returned the wrong transfer direction")
    if not isinstance(transfer_id, str) or len(transfer_id) != 32:
        raise ProtocolError("server returned an invalid transfer id")
    for name, value in (("size", size), ("chunks", chunks), ("chunk_size", chunk_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProtocolError("server returned invalid %s" % name)
    if chunk_size <= 0:
        raise ProtocolError("server returned invalid chunk_size")
    expected_chunks = (size + chunk_size - 1) // chunk_size if size else 0
    if chunks != expected_chunks:
        raise ProtocolError("server returned an inconsistent chunk count")
    return transfer_id, size, chunks, chunk_size


def _status_line(state: Mapping[str, Any]) -> str:
    size = int(state.get("size", 0))
    transferred = int(state.get("bytes_transferred", 0))
    percent = 100.0 if size == 0 else min(100.0, transferred * 100.0 / size)
    return "%s: %.1f%% (%d/%d bytes)" % (state.get("status", "unknown"), percent, transferred, size)


def wait_for_state(
    api: ApiClient,
    transfer_id: str,
    wanted: Sequence[str],
    timeout: int,
    quiet: bool,
    progress: Optional[_ProgressDisplay] = None,
    progress_label: Optional[str] = None,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_line: Optional[str] = None
    delay = 0.5
    while True:
        state = api.state(transfer_id)
        status = state.get("status")
        line = _status_line(state)
        if progress is not None:
            progress.update(
                progress_label or str(status or "unknown"),
                int(state.get("bytes_transferred", 0)),
                int(state.get("size", 0)),
                force=status in wanted,
            )
        elif not quiet and line != last_line:
            print(line, file=sys.stderr)
            last_line = line
        if status in wanted:
            return state
        if status == "error":
            raise ProtocolError("transfer failed on NAS: %s" % state.get("error", "unknown error"))
        if status not in ("awaiting_upload", "preparing", "receiving", "ready", "cleaning"):
            raise ProtocolError("server returned unknown transfer state: %r" % status)
        if time.monotonic() >= deadline:
            raise ProtocolError("timed out waiting for NAS transfer")
        time.sleep(delay)
        delay = min(1.0, delay * 1.4)


def upload(
    api: ApiClient,
    local_source: str,
    remote_destination: str,
    overwrite: bool,
    jobs: int,
    transfer_timeout: int,
    quiet: bool,
) -> None:
    source = Path(local_source)
    if not source.is_file():
        raise Nass3cpError("local source is not a regular file: %s" % source)
    before = source.stat()
    state = api.create_upload(remote_destination, before.st_size, before.st_mtime_ns, overwrite)
    transfer_id, size, chunks, chunk_size = _verify_state(state, "upload")
    committed = False
    progress = _ProgressDisplay(not quiet)
    try:
        digest = hashlib.sha256()
        transferred = 0
        progress.update("upload to S3", 0, size, force=True)
        with source.open("rb") as handle, ThreadPoolExecutor(max_workers=jobs) as executor:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
                or opened.st_mtime_ns != before.st_mtime_ns
            ):
                raise Nass3cpError("local source was replaced before it could be read")
            for start in range(0, chunks, jobs):
                count = min(jobs, chunks - start)
                items = api.urls(transfer_id, start, count)
                if len(items) != count:
                    raise ProtocolError("server returned the wrong number of upload URLs")
                payloads = []
                for offset in range(count):
                    data = handle.read(chunk_size)
                    if not data:
                        raise Nass3cpError("local source became shorter during transfer")
                    expected_index = start + offset
                    if items[offset].get("index") != expected_index:
                        raise ProtocolError("server returned out-of-order upload URLs")
                    digest.update(data)
                    payloads.append(data)
                batch_progress = _BatchProgress(
                    progress,
                    "upload to S3",
                    transferred,
                    size,
                    [len(payload) for payload in payloads],
                )
                futures = [
                    executor.submit(
                        _data_request,
                        "PUT",
                        item,
                        payload,
                        progress=None if quiet else batch_progress.callback(offset),
                    )
                    for offset, (item, payload) in enumerate(zip(items, payloads))
                ]
                for future, payload in zip(futures, payloads):
                    future.result()
                    transferred += len(payload)
                progress.update("upload to S3", transferred, size, force=transferred == size)
            if handle.read(1):
                raise Nass3cpError("local source grew during transfer")
            after = os.fstat(handle.fileno())
            if (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                raise Nass3cpError("local source changed during transfer")
        committed = True
        api.commit(transfer_id, digest.hexdigest())
        wait_for_state(
            api,
            transfer_id,
            ("complete",),
            transfer_timeout,
            quiet,
            progress,
            "copy from S3 to NAS",
        )
    except (Exception, KeyboardInterrupt):
        if not committed:
            api.abort(transfer_id)
        raise
    finally:
        progress.close()


def _local_destination(remote_source: str, value: str) -> Path:
    destination = Path(value)
    if destination.is_dir():
        name = PurePosixPath(remote_source).name
        if not name:
            raise Nass3cpError("cannot infer a filename from NAS source")
        destination = destination / name
    return destination


def download(
    api: ApiClient,
    remote_source: str,
    local_destination: str,
    overwrite: bool,
    jobs: int,
    transfer_timeout: int,
    quiet: bool,
) -> None:
    destination = _local_destination(remote_source, local_destination)
    if not destination.parent.is_dir():
        raise Nass3cpError("local destination directory does not exist: %s" % destination.parent)
    if destination.exists():
        if destination.is_dir():
            raise Nass3cpError("local destination is a directory: %s" % destination)
        if not overwrite:
            raise Nass3cpError("local destination exists; use --overwrite: %s" % destination)

    initial = api.create_download(remote_source)
    transfer_id, size, chunks, chunk_size = _verify_state(initial, "download")
    ready = False
    temporary: Optional[str] = None
    progress = _ProgressDisplay(not quiet)
    try:
        state = wait_for_state(
            api,
            transfer_id,
            ("ready",),
            transfer_timeout,
            quiet,
            progress,
            "copy from NAS to S3",
        )
        ready = True
        remote_digest = state.get("sha256")
        if not isinstance(remote_digest, str) or len(remote_digest) != 64:
            raise ProtocolError("server returned an invalid SHA-256 digest")
        fd, temporary = tempfile.mkstemp(
            prefix=".%s." % destination.name,
            suffix=".nass3cp-part",
            dir=str(destination.parent),
        )
        digest = hashlib.sha256()
        transferred = 0
        progress.update("download from S3", 0, size, force=True)
        with os.fdopen(fd, "wb") as handle, ThreadPoolExecutor(max_workers=jobs) as executor:
            for start in range(0, chunks, jobs):
                count = min(jobs, chunks - start)
                items = api.urls(transfer_id, start, count)
                if len(items) != count:
                    raise ProtocolError("server returned the wrong number of download URLs")
                request_specs = []
                for offset, item in enumerate(items):
                    index = start + offset
                    if item.get("index") != index:
                        raise ProtocolError("server returned out-of-order download URLs")
                    expected = min(chunk_size, size - index * chunk_size)
                    request_specs.append((item, expected))
                batch_progress = _BatchProgress(
                    progress,
                    "download from S3",
                    transferred,
                    size,
                    [expected for _, expected in request_specs],
                )
                futures = [
                    executor.submit(
                        _data_request,
                        "GET",
                        item,
                        None,
                        expected,
                        progress=None if quiet else batch_progress.callback(offset),
                    )
                    for offset, (item, expected) in enumerate(request_specs)
                ]
                for future in futures:
                    data = future.result()
                    handle.write(data)
                    digest.update(data)
                    transferred += len(data)
                progress.update("download from S3", transferred, size, force=transferred == size)
            handle.flush()
            os.fsync(handle.fileno())
        if transferred != size or digest.hexdigest() != remote_digest:
            raise Nass3cpError("end-to-end SHA-256 mismatch")
        if destination.exists() and not overwrite:
            raise Nass3cpError("local destination appeared during transfer; refusing to overwrite it")
        mtime_ns = state.get("mtime_ns")
        if isinstance(mtime_ns, int) and mtime_ns >= 0:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        os.replace(temporary, str(destination))
        temporary = None
        ready = False
        progress.close()
        try:
            api.acknowledge(transfer_id)
        except Nass3cpError as exc:
            if not quiet:
                print(
                    "warning: file is complete but NAS cleanup acknowledgement failed: %s" % exc,
                    file=sys.stderr,
                )
    except (Exception, KeyboardInterrupt):
        if ready:
            api.abort(transfer_id)
        raise
    finally:
        progress.close()
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
