import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import ServerConfig, load_server_config
from .errors import ConfigError, Nass3cpError, S3Error
from .s3 import S3Relay


LOG = logging.getLogger("nass3cp.server")
TRANSFER_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_FIELDS = (
    "id",
    "direction",
    "status",
    "size",
    "chunk_size",
    "chunks",
    "sha256",
    "mtime_ns",
    "bytes_transferred",
    "created_at",
    "updated_at",
    "error",
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class TransferStore:
    def __init__(self, state_dir: Path):
        self.directory = state_dir / "transfers"
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(state_dir), 0o700)
            os.chmod(str(self.directory), 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _filename(self, transfer_id: str) -> Path:
        return self.directory / (transfer_id + ".json")

    def _load(self) -> None:
        for filename in self.directory.glob("*.json"):
            try:
                with filename.open("r", encoding="utf-8") as handle:
                    state = json.load(handle)
                transfer_id = state.get("id")
                if not isinstance(transfer_id, str) or not TRANSFER_ID.fullmatch(transfer_id):
                    raise ValueError("invalid transfer id")
                if state.get("status") in ("preparing", "receiving", "cleaning"):
                    state["status"] = "error"
                    state["error"] = "server restarted while the transfer was active"
                    state["updated_at"] = time.time()
                self._save_locked(state)
                self._states[transfer_id] = state
            except Exception as exc:
                LOG.warning("ignoring invalid state file %s: %s", filename, exc)

    def _save_locked(self, state: Mapping[str, Any]) -> None:
        target = self._filename(str(state["id"]))
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(self.directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, str(target))
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def create(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            transfer_id = secrets.token_hex(16)
            now = time.time()
            state = dict(values)
            state.update({"id": transfer_id, "created_at": now, "updated_at": now})
            self._save_locked(state)
            self._states[transfer_id] = state
            return dict(state)

    def get(self, transfer_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return dict(self._states[transfer_id])
            except KeyError as exc:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "transfer not found") from exc

    def update(self, transfer_id: str, **values: Any) -> Dict[str, Any]:
        with self._lock:
            if transfer_id not in self._states:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "transfer not found")
            state = dict(self._states[transfer_id])
            state.update(values)
            state["updated_at"] = time.time()
            self._save_locked(state)
            self._states[transfer_id] = state
            return dict(state)

    def transition(
        self, transfer_id: str, expected: Iterable[str], new_status: str, **values: Any
    ) -> Dict[str, Any]:
        with self._lock:
            if transfer_id not in self._states:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "transfer not found")
            state = dict(self._states[transfer_id])
            if state.get("status") not in set(expected):
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "invalid_state",
                    "transfer is in state %s" % state.get("status"),
                )
            state.update(values)
            state["status"] = new_status
            state["updated_at"] = time.time()
            self._save_locked(state)
            self._states[transfer_id] = state
            return dict(state)

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(state) for state in self._states.values()]

    def remove(self, transfer_id: str) -> None:
        with self._lock:
            try:
                self._filename(transfer_id).unlink()
            except FileNotFoundError:
                pass
            self._states.pop(transfer_id, None)


def _public_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    return {name: state[name] for name in PUBLIC_FIELDS if name in state and state[name] is not None}


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except (OSError, ValueError):
        return False


class ServerApp:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.s3 = S3Relay(config.s3)
        self.store = TransferStore(config.state_dir)
        self._stop = threading.Event()

    def validate(self) -> None:
        validate_server_config(self.config)

    def authenticated(self, authorization: Optional[str]) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        token = authorization[len("Bearer ") :]
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, self.config.auth_token_sha256)

    def resolve_remote(self, requested: str, write: bool, overwrite: bool = False) -> Path:
        if not requested or "\x00" in requested:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_path", "invalid NAS path")
        raw = Path(requested)
        if raw.is_absolute():
            candidate = raw.resolve(strict=False)
        else:
            candidate = (self.config.allowed_roots[0] / raw).resolve(strict=False)
        if not any(_inside(candidate, root) for root in self.config.allowed_roots):
            raise ApiError(HTTPStatus.FORBIDDEN, "path_not_allowed", "NAS path is outside allowed roots")
        if write:
            if not candidate.parent.is_dir():
                raise ApiError(HTTPStatus.BAD_REQUEST, "parent_missing", "destination directory does not exist")
            if candidate.exists():
                if candidate.is_dir():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "not_a_file", "destination is a directory")
                if not overwrite:
                    raise ApiError(HTTPStatus.CONFLICT, "destination_exists", "destination already exists; use --overwrite")
        elif not candidate.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "source is not a regular file")
        return candidate

    def create_upload(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        size = body.get("size")
        overwrite = body.get("overwrite", False)
        mtime_ns = body.get("mtime_ns")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "size must be a non-negative integer")
        if size > self.config.max_file_size:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file_too_large", "file exceeds server max_file_size")
        if not isinstance(overwrite, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "overwrite must be a boolean")
        if mtime_ns is not None and (isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int) or mtime_ns < 0):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "mtime_ns must be a non-negative integer")
        destination = self.resolve_remote(requested, write=True, overwrite=overwrite)
        chunks = (size + self.config.chunk_size - 1) // self.config.chunk_size if size else 0
        return self.store.create(
            {
                "direction": "upload",
                "status": "awaiting_upload",
                "path": str(destination),
                "size": size,
                "mtime_ns": mtime_ns,
                "overwrite": overwrite,
                "chunk_size": self.config.chunk_size,
                "chunks": chunks,
                "bytes_transferred": 0,
                "sha256": None,
                "error": None,
                "objects_cleaned": chunks == 0,
            }
        )

    def create_download(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        source = self.resolve_remote(requested, write=False)
        stat = source.stat()
        if stat.st_size > self.config.max_file_size:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file_too_large", "file exceeds server max_file_size")
        chunks = (
            (stat.st_size + self.config.chunk_size - 1) // self.config.chunk_size
            if stat.st_size
            else 0
        )
        state = self.store.create(
            {
                "direction": "download",
                "status": "preparing",
                "path": str(source),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "source_dev": stat.st_dev,
                "source_ino": stat.st_ino,
                "chunk_size": self.config.chunk_size,
                "chunks": chunks,
                "bytes_transferred": 0,
                "sha256": None,
                "error": None,
                "objects_cleaned": chunks == 0,
            }
        )
        self.start_worker(self._prepare_download, str(state["id"]))
        return state

    def urls(self, transfer_id: str, start: int, count: int) -> Dict[str, Any]:
        state = self.store.get(transfer_id)
        direction = state.get("direction")
        if direction == "upload":
            if state.get("status") != "awaiting_upload":
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "upload URLs are no longer available")
            method = "PUT"
        elif direction == "download":
            if state.get("status") != "ready":
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "download is not ready")
            method = "GET"
        else:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "invalid_state", "invalid transfer direction")
        chunks = int(state["chunks"])
        if start < 0 or count <= 0 or count > 128 or start > chunks:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_range", "invalid URL batch range")
        end = min(start + count, chunks)
        items = []
        for index in range(start, end):
            expected = min(
                int(state["chunk_size"]),
                max(0, int(state["size"]) - index * int(state["chunk_size"])),
            )
            signed = self.s3.presign_chunk(
                method,
                transfer_id,
                index,
                content_length=expected if method == "PUT" else None,
            )
            items.append({"index": index, "url": signed.url, "headers": signed.headers})
        return {"items": items, "next": end if end < chunks else None}

    def commit_upload(self, transfer_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        digest = body.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "sha256 must be lowercase hexadecimal")
        current = self.store.get(transfer_id)
        if current.get("direction") != "upload":
            raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "not an upload transfer")
        if current.get("status") in ("receiving", "complete"):
            if current.get("sha256") != digest:
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "upload was committed with a different digest")
            return current
        state = self.store.transition(
            transfer_id,
            ("awaiting_upload",),
            "receiving",
            sha256=digest,
            bytes_transferred=0,
            error=None,
        )
        self.start_worker(self._receive_upload, transfer_id)
        return state

    def acknowledge_download(self, transfer_id: str) -> Dict[str, Any]:
        state = self.store.get(transfer_id)
        if state.get("direction") != "download":
            raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "not a download transfer")
        if state.get("status") in ("cleaning", "complete"):
            return state
        state = self.store.transition(transfer_id, ("ready",), "cleaning")
        self.start_worker(self._cleanup_complete, transfer_id)
        return state

    def abort(self, transfer_id: str) -> Dict[str, Any]:
        state = self.store.transition(
            transfer_id,
            ("awaiting_upload", "ready", "error"),
            "cleaning",
            error="transfer aborted by client",
        )
        self.start_worker(self._cleanup_error, transfer_id, "transfer aborted by client")
        return state

    def start_worker(self, function: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(target=function, args=args, daemon=True)
        thread.start()

    @staticmethod
    def _retry(function: Callable[[], Any], description: str, attempts: int = 4) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return function()
            except (S3Error, OSError, HTTPException) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
        raise S3Error("%s failed after %d attempts: %s" % (description, attempts, last_error))

    def _get_chunk_bytes(self, transfer_id: str, index: int, expected: int) -> bytes:
        def operation() -> bytes:
            response = self.s3.get_chunk(transfer_id, index)
            try:
                data = response.read(expected + 1)
            finally:
                response.close()
            if len(data) != expected:
                raise S3Error("chunk %d has size %d, expected %d" % (index, len(data), expected))
            return data

        return self._retry(operation, "download chunk %d" % index)

    @staticmethod
    def _upload_temporary_path(state: Mapping[str, Any]) -> Path:
        destination = Path(str(state["path"]))
        return destination.parent / (".nass3cp-%s.part" % state["id"])

    def _remove_upload_temporary(self, state: Mapping[str, Any]) -> None:
        if state.get("direction") != "upload":
            return
        try:
            self._upload_temporary_path(state).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("could not remove partial NAS file for %s: %s", state["id"], exc)

    def _revalidate_upload_destination(self, destination: Path) -> None:
        current = destination.resolve(strict=False)
        if current != destination or not any(
            _inside(current, root) for root in self.config.allowed_roots
        ):
            raise Nass3cpError("destination path changed or left an allowed root")
        if not destination.parent.is_dir():
            raise Nass3cpError("destination directory disappeared during transfer")

    def _receive_upload(self, transfer_id: str) -> None:
        state = self.store.get(transfer_id)
        destination = Path(str(state["path"]))
        temporary = self._upload_temporary_path(state)
        try:
            self._revalidate_upload_destination(destination)
            fd = os.open(
                str(temporary),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            digest = hashlib.sha256()
            transferred = 0
            with os.fdopen(fd, "wb") as handle:
                try:
                    os.chmod(str(temporary), 0o600)
                except OSError:
                    pass
                for index in range(int(state["chunks"])):
                    expected = min(int(state["chunk_size"]), int(state["size"]) - transferred)
                    data = self._get_chunk_bytes(transfer_id, index, expected)
                    handle.write(data)
                    digest.update(data)
                    transferred += len(data)
                    self.store.update(transfer_id, bytes_transferred=transferred)
                handle.flush()
                os.fsync(handle.fileno())
            if digest.hexdigest() != state["sha256"]:
                raise Nass3cpError("end-to-end SHA-256 mismatch")
            self._revalidate_upload_destination(destination)
            if destination.exists() and not state.get("overwrite"):
                raise Nass3cpError("destination appeared during transfer; refusing to overwrite it")
            if state.get("mtime_ns") is not None:
                os.utime(
                    str(temporary),
                    ns=(int(state["mtime_ns"]), int(state["mtime_ns"])),
                )
            os.replace(str(temporary), str(destination))
            self.store.update(
                transfer_id,
                status="complete",
                bytes_transferred=transferred,
                error=None,
            )
            cleaned = self._cleanup_objects(transfer_id, int(state["chunks"]))
            self.store.update(transfer_id, objects_cleaned=cleaned)
            LOG.info("upload %s completed", transfer_id)
        except Exception as exc:
            LOG.error("upload %s failed: %s", transfer_id, exc)
            self.store.update(transfer_id, status="error", error=str(exc))
            cleaned = self._cleanup_objects(transfer_id, int(state["chunks"]))
            self.store.update(transfer_id, objects_cleaned=cleaned)
        finally:
            self._remove_upload_temporary(state)

    def _prepare_download(self, transfer_id: str) -> None:
        state = self.store.get(transfer_id)
        source = Path(str(state["path"]))
        try:
            digest = hashlib.sha256()
            transferred = 0
            with source.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if before.st_dev != state["source_dev"] or before.st_ino != state["source_ino"]:
                    raise Nass3cpError("source file was replaced before it could be read")
                for index in range(int(state["chunks"])):
                    data = handle.read(int(state["chunk_size"]))
                    if not data:
                        raise Nass3cpError("source file became shorter during transfer")
                    self._retry(
                        lambda current=index, payload=data: self.s3.put_chunk(transfer_id, current, payload),
                        "upload chunk %d" % index,
                    )
                    digest.update(data)
                    transferred += len(data)
                    self.store.update(transfer_id, bytes_transferred=transferred)
                if handle.read(1):
                    raise Nass3cpError("source file grew during transfer")
                after = os.fstat(handle.fileno())
                if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                    raise Nass3cpError("source file changed during transfer")
            self.store.update(
                transfer_id,
                status="ready",
                bytes_transferred=transferred,
                sha256=digest.hexdigest(),
                error=None,
            )
            LOG.info("download %s is ready", transfer_id)
        except Exception as exc:
            LOG.error("download preparation %s failed: %s", transfer_id, exc)
            self.store.update(transfer_id, status="error", error=str(exc))
            cleaned = self._cleanup_objects(transfer_id, int(state["chunks"]))
            self.store.update(transfer_id, objects_cleaned=cleaned)

    def _cleanup_objects(self, transfer_id: str, chunks: int) -> bool:
        try:
            errors = self.s3.cleanup(transfer_id, chunks)
        except Exception as exc:
            LOG.warning("object cleanup for %s failed: %s", transfer_id, exc)
            return False
        if errors:
            LOG.warning("could not clean %d object(s) for %s: %s", len(errors), transfer_id, errors[0])
            return False
        return True

    def _cleanup_complete(self, transfer_id: str) -> None:
        state = self.store.get(transfer_id)
        cleaned = self._cleanup_objects(transfer_id, int(state["chunks"]))
        self.store.update(
            transfer_id, status="complete", error=None, objects_cleaned=cleaned
        )

    def _cleanup_error(self, transfer_id: str, message: str) -> None:
        state = self.store.get(transfer_id)
        cleaned = self._cleanup_objects(transfer_id, int(state["chunks"]))
        self.store.update(
            transfer_id, status="error", error=message, objects_cleaned=cleaned
        )

    def cleanup_interrupted(self) -> None:
        for state in self.store.all():
            if state.get("status") not in ("complete", "error") or state.get("objects_cleaned"):
                continue
            transfer_id = str(state["id"])
            self._remove_upload_temporary(state)
            cleaned = self._cleanup_objects(transfer_id, int(state["chunks"]))
            self.store.update(transfer_id, objects_cleaned=cleaned)

    def janitor(self) -> None:
        while not self._stop.wait(60):
            cutoff = time.time() - self.config.transfer_ttl_seconds
            for state in self.store.all():
                if float(state.get("updated_at", 0)) >= cutoff:
                    continue
                transfer_id = str(state["id"])
                status = state.get("status")
                if status in ("preparing", "receiving", "cleaning"):
                    continue
                self._remove_upload_temporary(state)
                if not state.get("objects_cleaned"):
                    self._cleanup_objects(transfer_id, int(state["chunks"]))
                self.store.remove(transfer_id)

    def stop(self) -> None:
        self._stop.set()


class Nass3cpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], handler: Any, app: ServerApp):
        super().__init__(address, handler)
        self.app = app


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "nass3cp/%s" % __version__
    sys_version = ""

    @property
    def app(self) -> ServerApp:
        return self.server.app  # type: ignore[attr-defined,no-any-return]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format_string % args)

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, error: ApiError) -> None:
        self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

    def _authorize(self) -> bool:
        if self.app.authenticated(self.headers.get("Authorization")):
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": {"code": "unauthorized", "message": "valid bearer token required"}},
        )
        return False

    def _body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid Content-Length") from exc
        if length < 0 or length > 65536:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "request body is too large")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        if not self._authorize():
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/health":
                self._send_json(HTTPStatus.OK, {"status": "ok", "version": __version__})
                return
            match = re.fullmatch(r"/v1/transfers/([0-9a-f]{32})", parsed.path)
            if match:
                self._send_json(HTTPStatus.OK, _public_state(self.app.store.get(match.group(1))))
                return
            match = re.fullmatch(r"/v1/transfers/([0-9a-f]{32})/urls", parsed.path)
            if match:
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    start = int(query.get("start", ["0"])[0])
                    count = int(query.get("count", ["128"])[0])
                except ValueError as exc:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_range", "start and count must be integers") from exc
                self._send_json(HTTPStatus.OK, self.app.urls(match.group(1), start, count))
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
        except ApiError as exc:
            self._error(exc)
        except Exception:
            LOG.exception("unhandled GET error")
            self._error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "internal server error"))

    def do_POST(self) -> None:
        if not self._authorize():
            return
        try:
            path = urlsplit(self.path).path
            body = self._body()
            if path == "/v1/transfers/upload":
                state = self.app.create_upload(body)
                self._send_json(HTTPStatus.CREATED, _public_state(state))
                return
            if path == "/v1/transfers/download":
                state = self.app.create_download(body)
                self._send_json(HTTPStatus.ACCEPTED, _public_state(state))
                return
            match = re.fullmatch(r"/v1/transfers/([0-9a-f]{32})/(commit|ack|abort)", path)
            if match:
                transfer_id, action = match.groups()
                if action == "commit":
                    state = self.app.commit_upload(transfer_id, body)
                elif action == "ack":
                    state = self.app.acknowledge_download(transfer_id)
                else:
                    state = self.app.abort(transfer_id)
                self._send_json(HTTPStatus.ACCEPTED, _public_state(state))
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
        except ApiError as exc:
            self._error(exc)
        except Exception:
            LOG.exception("unhandled POST error")
            self._error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "internal server error"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the nass3cp NAS-side service")
    parser.add_argument("--config", required=True, help="server JSON configuration file")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument(
        "--check-config", action="store_true", help="validate configuration and exit"
    )
    checks.add_argument(
        "--check-s3",
        action="store_true",
        help="write, read, and delete a small relay object, then exit",
    )
    parser.add_argument("--verbose", action="store_true", help="enable verbose logging")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def check_s3(config: ServerConfig) -> None:
    """Verify the configured relay with a tiny, automatically removed object."""
    relay = S3Relay(config.s3)
    transfer_id = secrets.token_hex(16)
    payload = b"nass3cp S3 relay check\n"
    primary_error: Optional[Exception] = None
    try:
        relay.put_chunk(transfer_id, 0, payload)
        response = relay.get_chunk(transfer_id, 0)
        try:
            received = response.read(len(payload) + 1)
        finally:
            response.close()
        if received != payload:
            raise S3Error("S3 relay check returned different data")
    except Exception as exc:
        primary_error = exc

    cleanup_errors = relay.cleanup(transfer_id, 1)
    if primary_error is not None:
        raise primary_error
    if cleanup_errors:
        raise S3Error("S3 relay check could not delete its object: %s" % cleanup_errors[0])


def validate_server_config(config: ServerConfig) -> ssl.SSLContext:
    if not config.cert_file.is_file():
        raise ConfigError("TLS certificate does not exist: %s" % config.cert_file)
    if not config.key_file.is_file():
        raise ConfigError("TLS private key does not exist: %s" % config.key_file)
    for root in config.allowed_roots:
        if not root.is_dir():
            raise ConfigError("allowed root is not a directory: %s" % root)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(config.cert_file), str(config.key_file))
    return context


def run(config: ServerConfig) -> None:
    context = validate_server_config(config)
    app = ServerApp(config)
    server = Nass3cpHTTPServer((config.listen, config.port), RequestHandler, app)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    janitor = threading.Thread(target=app.janitor, daemon=True)
    janitor.start()
    app.start_worker(app.cleanup_interrupted)
    LOG.info("listening with TLS on %s:%d", config.listen, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.stop()
        server.server_close()


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = load_server_config(args.config)
        if args.check_config:
            validate_server_config(config)
            print("configuration is valid")
            return
        if args.check_s3:
            check_s3(config)
            print("S3 relay check passed (PUT, GET, DELETE)")
            return
        run(config)
    except KeyboardInterrupt:
        return
    except (Nass3cpError, OSError) as exc:
        print("nass3cp-server: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
