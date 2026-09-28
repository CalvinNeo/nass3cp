"""Whole-file NAS HTTP transfers, serialized over an authenticated nathole tunnel."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from .errors import DownloadCancelled, Nass3cpError, UploadCancelled


BLOCK = 64 * 1024


def _remote_path(handler):
    from .server import ApiError
    try:
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True, errors="surrogatepass",
                         max_num_fields=4)
    except ValueError as exc:
        raise ApiError(400, "invalid_path", "invalid file query") from exc
    paths = query.get("path", [])
    if len(paths) != 1 or not paths[0] or len(paths[0]) > 8192:
        raise ApiError(400, "invalid_path", "one file path is required")
    return paths[0]


def serve_download(handler):
    from .server import ApiError
    app = handler.app
    if not app.direct_lock.acquire(blocking=False):
        raise ApiError(409, "direct_busy", "another direct file transfer is in progress")
    try:
        path = app.resolve_remote(_remote_path(handler), write=False)
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > app.config.max_file_size:
                raise ApiError(400, "invalid_file", "source is not an allowed regular file")
            digest = hashlib.sha256()
            while True:
                if app._stop.is_set():
                    raise ApiError(503, "closing", "NAS service is closing")
                data = source.read(BLOCK)
                if not data:
                    break
                digest.update(data)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ApiError(409, "source_changed", "source changed while preparing the download")
            source.seek(0)
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(before.st_size))
            handler.send_header("X-Nass3cp-SHA256", digest.hexdigest())
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.end_headers()
            remaining = before.st_size
            try:
                while remaining and not app._stop.is_set():
                    data = source.read(min(BLOCK, remaining))
                    if not data:
                        break
                    handler.wfile.write(data)
                    remaining -= len(data)
            except (OSError, TimeoutError):
                pass  # The downloader rejects truncated or corrupt content.
            handler.close_connection = True
    finally:
        app.direct_lock.release()


def receive_upload(handler):
    from .server import ApiError
    app = handler.app
    lengths = handler.headers.get_all("Content-Length", [])
    if (handler.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,20}", lengths[0])):
        raise ApiError(400, "invalid_length", "one Content-Length is required")
    length = int(lengths[0])
    digest_header = handler.headers.get_all("X-Nass3cp-SHA256", [])
    if len(digest_header) != 1 or not re.fullmatch(r"[0-9a-f]{64}", digest_header[0]):
        raise ApiError(400, "invalid_digest", "a SHA-256 digest is required")
    if length > app.config.max_file_size:
        raise ApiError(413, "file_too_large", "file exceeds max_file_size")
    raw_mtime = handler.headers.get("X-Nass3cp-Mtime-Ns", "0")
    if not re.fullmatch(r"[0-9]{1,19}", raw_mtime) or int(raw_mtime) > 2**63 - 1:
        raise ApiError(400, "invalid_mtime", "invalid modification time")
    requested = _remote_path(handler)
    if not app.direct_lock.acquire(blocking=False):
        raise ApiError(409, "direct_busy", "another direct file transfer is in progress")
    temporary = None
    try:
        destination = app.resolve_remote(requested, write=True, overwrite=False)
        if shutil.disk_usage(str(destination.parent)).free < length:
            raise ApiError(507, "no_space", "not enough free space on the NAS")
        descriptor, name = tempfile.mkstemp(prefix=".nass3cp-direct-", suffix=".part", dir=str(destination.parent))
        temporary = Path(name)
        digest, remaining = hashlib.sha256(), length
        with os.fdopen(descriptor, "wb") as output:
            while remaining:
                if app._stop.is_set():
                    raise ApiError(503, "closing", "NAS service is closing")
                data = handler.rfile.read(min(BLOCK, remaining))
                if not data:
                    raise ApiError(400, "incomplete_upload", "upload was interrupted")
                output.write(data)
                digest.update(data)
                remaining -= len(data)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != digest_header[0]:
            raise ApiError(400, "digest_mismatch", "upload SHA-256 does not match")
        if app.resolve_remote(requested, write=True, overwrite=False) != destination:
            raise ApiError(409, "destination_changed", "destination changed during upload")
        if int(raw_mtime):
            os.utime(str(temporary), ns=(int(raw_mtime), int(raw_mtime)))
        try:
            # Atomically publish without replacing a destination created by another request.
            os.link(str(temporary), str(destination))
        except FileExistsError as exc:
            raise ApiError(409, "destination_exists", "destination already exists") from exc
        handler._send_json(201, {"status": "complete", "size": length, "sha256": digest.hexdigest()})
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        app.direct_lock.release()


class DirectTransfers:
    def __init__(self, tunnel):
        self.tunnel = tunnel
        self.lock = threading.Lock()

    @contextmanager
    def _connection(self, cancel, cancelled, progress):
        progress("waiting for nathole", 0, 0)
        while not self.lock.acquire(timeout=0.1):
            if cancel.is_set():
                raise cancelled("Transfer cancelled")
        try:
            if cancel.is_set():
                raise cancelled("Transfer cancelled")
            yield self.tunnel.endpoint(cancel)
        except HTTPError as exc:
            if cancel.is_set():
                raise cancelled("Transfer cancelled") from exc
            try:
                message = json.loads(exc.read(65536).decode("utf-8"))["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = "HTTP %d" % exc.code
            finally:
                exc.close()
            raise Nass3cpError("NAS refused the file transfer: %s" % message) from exc
        except (OSError, URLError) as exc:
            if cancel.is_set():
                raise cancelled("Transfer cancelled") from exc
            raise Nass3cpError("nathole file transfer failed; retry the file or select S3") from exc
        finally:
            self.lock.release()

    def download(self, path, destination, progress, cancel):
        with self._connection(cancel, DownloadCancelled, progress) as api:
            request = Request(api.base_url + "/v1/files?" + urlencode({"path": path}, errors="surrogatepass"),
                              headers={"Authorization": "Bearer " + api.password})
            with api.opener.open(request, timeout=60) as response:
                raw_size = response.headers.get("Content-Length", "")
                expected = response.headers.get("X-Nass3cp-SHA256", "")
                if not re.fullmatch(r"[0-9]{1,20}", raw_size) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise Nass3cpError("NAS returned invalid direct-download metadata")
                size = int(raw_size)
                target = Path(destination)
                if shutil.disk_usage(str(target.parent)).free < size:
                    raise Nass3cpError("not enough local space for this download")
                digest, received = hashlib.sha256(), 0
                created = False
                try:
                    with target.open("xb") as output:
                        created = True
                        while received < size:
                            if cancel.is_set():
                                raise DownloadCancelled("Download cancelled")
                            data = response.read(min(BLOCK, size - received))
                            if not data:
                                raise Nass3cpError("NAS download was interrupted")
                            output.write(data)
                            digest.update(data)
                            received += len(data)
                            progress("download via nathole", received, size)
                    progress("verify download", received, size)
                    if cancel.is_set():
                        raise DownloadCancelled("Download cancelled")
                    if digest.hexdigest() != expected:
                        raise Nass3cpError("NAS download SHA-256 does not match")
                except Exception:
                    if created:
                        try:
                            target.unlink()
                        except FileNotFoundError:
                            pass
                    raise

    def upload(self, source, path, mtime_ns, progress, cancel):
        with self._connection(cancel, UploadCancelled, progress) as api:
            source = Path(source)
            size = source.stat().st_size
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                while True:
                    if cancel.is_set():
                        raise UploadCancelled("Upload cancelled")
                    data = handle.read(BLOCK)
                    if not data:
                        break
                    digest.update(data)
                handle.seek(0)
                class Reader:
                    sent = 0

                    def read(self, amount=BLOCK):
                        if cancel.is_set():
                            raise UploadCancelled("Upload cancelled")
                        value = handle.read(min(amount, BLOCK))
                        self.sent += len(value)
                        progress("upload via nathole", self.sent, size)
                        return value
                request = Request(api.base_url + "/v1/files?" + urlencode({"path": path}, errors="surrogatepass"),
                                  method="PUT", data=Reader(), headers={
                                      "Authorization": "Bearer " + api.password,
                                      "Content-Type": "application/octet-stream", "Content-Length": str(size),
                                      "X-Nass3cp-SHA256": digest.hexdigest(),
                                      "X-Nass3cp-Mtime-Ns": str(mtime_ns),
                                  })
                with api.opener.open(request, timeout=60) as response:
                    value = json.loads(response.read(65537).decode("utf-8"))
                if (value.get("status") != "complete" or value.get("size") != size
                        or value.get("sha256") != digest.hexdigest()):
                    raise Nass3cpError("NAS did not confirm the complete upload")
                progress("upload verified", size, size)

    def close(self):
        self.tunnel.close()
