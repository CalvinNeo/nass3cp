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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .errors import Nass3cpError, ProtocolError
from .net import secure_opener


_DATA_OPENERS = threading.local()


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
        token: str,
        ca_file: Optional[str] = None,
        insecure: bool = False,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
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
            "Authorization": "Bearer " + self.token,
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
        request = Request(url, data=data, headers=clean_headers, method=method)
        try:
            response = _data_opener().open(request, timeout=300)
            try:
                if method == "GET":
                    result = response.read((expected + 1) if expected is not None else -1)
                else:
                    response.read()
                    result = b""
            finally:
                response.close()
            if expected is not None and len(result) != expected:
                raise ProtocolError(
                    "S3 chunk has size %d, expected %d" % (len(result), expected)
                )
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
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_line: Optional[str] = None
    delay = 0.5
    while True:
        state = api.state(transfer_id)
        status = state.get("status")
        line = _status_line(state)
        if not quiet and line != last_line:
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
        delay = min(5.0, delay * 1.4)


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
    try:
        digest = hashlib.sha256()
        transferred = 0
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
                futures = [
                    executor.submit(_data_request, "PUT", item, payload)
                    for item, payload in zip(items, payloads)
                ]
                for future, payload in zip(futures, payloads):
                    future.result()
                    transferred += len(payload)
                if not quiet:
                    percent = 100.0 if size == 0 else transferred * 100.0 / size
                    print("upload to S3: %.1f%% (%d/%d bytes)" % (percent, transferred, size), file=sys.stderr)
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
        wait_for_state(api, transfer_id, ("complete",), transfer_timeout, quiet)
    except (Exception, KeyboardInterrupt):
        if not committed:
            api.abort(transfer_id)
        raise


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
    try:
        state = wait_for_state(api, transfer_id, ("ready",), transfer_timeout, quiet)
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
        with os.fdopen(fd, "wb") as handle, ThreadPoolExecutor(max_workers=jobs) as executor:
            for start in range(0, chunks, jobs):
                count = min(jobs, chunks - start)
                items = api.urls(transfer_id, start, count)
                if len(items) != count:
                    raise ProtocolError("server returned the wrong number of download URLs")
                futures = []
                for offset, item in enumerate(items):
                    index = start + offset
                    if item.get("index") != index:
                        raise ProtocolError("server returned out-of-order download URLs")
                    expected = min(chunk_size, size - index * chunk_size)
                    futures.append(executor.submit(_data_request, "GET", item, None, expected))
                for future in futures:
                    data = future.result()
                    handle.write(data)
                    digest.update(data)
                    transferred += len(data)
                if not quiet:
                    percent = 100.0 if size == 0 else transferred * 100.0 / size
                    print("download from S3: %.1f%% (%d/%d bytes)" % (percent, transferred, size), file=sys.stderr)
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
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
