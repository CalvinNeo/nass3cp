import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import ssl
import stat
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
from .compression import DecodingWriter, gzip_compress_stream
from .config import ServerConfig, load_environment_file, load_server_config
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
    "pipeline",
    "inflight",
    "chunks_staged",
    "chunks_consumed",
    "producer_complete",
    "compression",
    "decoded_size",
    "decoded_sha256",
    "metadata_ready",
    "resumable",
    "resumed",
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
            for attempt in range(5):
                try:
                    os.replace(temporary, str(target))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (2 ** attempt))
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
        self._pipeline_condition = threading.Condition()

    def validate(self) -> None:
        validate_server_config(self.config)

    def authenticated(self, authorization: Optional[str]) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        password = authorization[len("Bearer ") :]
        actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, self.config.auth_password_sha256)

    def _remote_lexical_candidate(self, requested: str) -> Path:
        if not requested or "\x00" in requested:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_path", "invalid NAS path")
        raw = Path(requested)
        if raw.is_absolute():
            return raw
        return self.config.allowed_roots[0] / raw

    def _remote_candidate(self, requested: str) -> Path:
        candidate = self._remote_lexical_candidate(requested).resolve(strict=False)
        if not any(_inside(candidate, root) for root in self.config.allowed_roots):
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "path_not_allowed",
                "NAS path is outside allowed roots",
            )
        return candidate

    def resolve_remote(self, requested: str, write: bool, overwrite: bool = False) -> Path:
        lexical = self._remote_lexical_candidate(requested)
        candidate = self._remote_candidate(requested)
        try:
            lexical_details = lexical.lstat()
        except FileNotFoundError:
            lexical_details = None
        if write:
            if not candidate.parent.is_dir():
                raise ApiError(HTTPStatus.BAD_REQUEST, "parent_missing", "destination directory does not exist")
            if lexical_details is not None:
                if not stat.S_ISREG(lexical_details.st_mode):
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "not_a_file",
                        "destination is not a regular file",
                    )
                if not overwrite:
                    raise ApiError(HTTPStatus.CONFLICT, "destination_exists", "destination already exists; use --overwrite")
        elif lexical_details is None or not stat.S_ISREG(lexical_details.st_mode):
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "source is not a regular file")
        return candidate

    def resolve_remote_directory(self, requested: str) -> Path:
        normalized = requested or "."
        lexical = self._remote_lexical_candidate(normalized)
        candidate = self._remote_candidate(normalized)
        try:
            details = lexical.lstat()
        except FileNotFoundError:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "NAS directory does not exist")
        if not stat.S_ISDIR(details.st_mode):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "not_a_directory",
                "NAS path is not a directory",
            )
        return candidate

    def path_info(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        lexical = self._remote_lexical_candidate(requested)
        candidate = self._remote_candidate(requested)
        try:
            details = lexical.lstat()
        except FileNotFoundError:
            return {"exists": False}
        except PermissionError as exc:
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "permission_denied",
                "permission denied while inspecting NAS path",
            ) from exc
        if stat.S_ISDIR(details.st_mode):
            kind = "directory"
        elif stat.S_ISREG(details.st_mode):
            kind = "file"
        else:
            kind = "other"
        return {
            "exists": True,
            "type": kind,
            "size": details.st_size if kind == "file" else None,
            "mtime_ns": details.st_mtime_ns,
        }

    def ensure_directory(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        lexical = self._remote_lexical_candidate(requested)
        candidate = self._remote_candidate(requested)
        try:
            details = lexical.lstat()
        except FileNotFoundError:
            details = None
        if details is not None:
            if not stat.S_ISDIR(details.st_mode):
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "path_conflict",
                    "NAS directory path already exists as a non-directory",
                )
            return {"created": False}
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            resolved = candidate.resolve(strict=True)
        except FileExistsError as exc:
            try:
                raced_details = lexical.lstat()
            except OSError:
                raced_details = None
            if raced_details is not None and stat.S_ISDIR(raced_details.st_mode):
                return {"created": False}
            raise ApiError(
                HTTPStatus.CONFLICT,
                "path_conflict",
                "NAS directory path already exists as a non-directory",
            ) from exc
        except PermissionError as exc:
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "permission_denied",
                "permission denied while creating NAS directory",
            ) from exc
        except OSError as exc:
            raise ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "mkdir_failed",
                "could not create NAS directory",
            ) from exc
        if not any(_inside(resolved, root) for root in self.config.allowed_roots):
            try:
                candidate.rmdir()
            except OSError:
                pass
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "path_not_allowed",
                "created directory left an allowed root",
            )
        return {"created": True}

    def list_directory(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        cursor = body.get("cursor", 0)
        limit = body.get("limit", 500)
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "cursor must be a non-negative integer",
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > 1000
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "limit must be an integer between 1 and 1000",
            )

        directory = self.resolve_remote_directory(requested)
        entries: List[Dict[str, Any]] = []
        try:
            with os.scandir(str(directory)) as iterator:
                for entry in iterator:
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        # A concurrently removed entry is simply absent from this page.
                        continue
                    mode = details.st_mode
                    if stat.S_ISDIR(mode):
                        kind = "directory"
                    elif stat.S_ISREG(mode):
                        kind = "file"
                    elif stat.S_ISLNK(mode):
                        kind = "symlink"
                    else:
                        kind = "other"
                    entries.append(
                        {
                            "name": entry.name,
                            "type": kind,
                            "size": details.st_size if kind == "file" else None,
                            "mtime_ns": details.st_mtime_ns,
                        }
                    )
        except FileNotFoundError as exc:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "NAS directory disappeared while it was being listed",
            ) from exc
        except NotADirectoryError as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "not_a_directory",
                "NAS path is not a directory",
            ) from exc
        except PermissionError as exc:
            raise ApiError(
                HTTPStatus.FORBIDDEN,
                "permission_denied",
                "permission denied while listing NAS directory",
            ) from exc
        except OSError as exc:
            raise ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "list_failed",
                "could not list NAS directory",
            ) from exc

        entries.sort(key=lambda item: (str(item["name"]).casefold(), str(item["name"])))
        end = min(len(entries), cursor + limit)
        return {
            "path": requested or ".",
            "entries": entries[cursor:end],
            "next_cursor": end if end < len(entries) else None,
            "total": len(entries),
        }

    def create_upload(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        size = body.get("size")
        overwrite = body.get("overwrite", False)
        mtime_ns = body.get("mtime_ns")
        compression = body.get("compression")
        decoded_size = body.get("decoded_size")
        resumable = body.get("resume", False)
        resume_id = body.get("resume_id")
        pipeline = "inflight" in body
        inflight = body.get("inflight")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "size must be a non-negative integer")
        if size > self.config.max_file_size:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file_too_large", "file exceeds server max_file_size")
        if not isinstance(overwrite, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "overwrite must be a boolean")
        if not isinstance(resumable, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "resume must be a boolean")
        if resume_id is not None and (
            not resumable
            or not isinstance(resume_id, str)
            or not TRANSFER_ID.fullmatch(resume_id)
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "resume_id must be a transfer id and requires resume",
            )
        if resumable and pipeline:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "resumable uploads cannot use the bounded pipeline",
            )
        if mtime_ns is not None and (isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int) or mtime_ns < 0):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "mtime_ns must be a non-negative integer")
        if pipeline and (
            isinstance(inflight, bool)
            or not isinstance(inflight, int)
            or inflight <= 0
            or inflight > 128
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "inflight must be an integer between 1 and 128",
            )
        if compression not in (None, "gzip"):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "compression must be gzip when provided",
            )
        if compression == "gzip":
            if not pipeline:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "compressed uploads require the pipeline protocol",
                )
            if (
                isinstance(decoded_size, bool)
                or not isinstance(decoded_size, int)
                or decoded_size < 0
                or decoded_size > self.config.max_file_size
            ):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "decoded_size must be within the server file size limit",
                )
        elif decoded_size is not None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "decoded_size requires gzip compression",
            )
        if resumable and compression is not None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "resumable uploads cannot use compression",
            )

        requested_destination = self._remote_candidate(requested)
        if resume_id is not None:
            try:
                previous = self.store.get(resume_id)
            except ApiError as exc:
                if exc.code != "not_found":
                    raise
                previous = None
            if previous is not None:
                matches = (
                    previous.get("direction") == "upload"
                    and previous.get("resumable") is True
                    and previous.get("pipeline") is not True
                    and previous.get("compression") is None
                    and previous.get("path") == str(requested_destination)
                    and previous.get("size") == size
                    and previous.get("mtime_ns") == mtime_ns
                    and previous.get("overwrite") == overwrite
                )
                if not matches:
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "resume_mismatch",
                        "saved upload does not match this source and destination",
                    )
                status = previous.get("status")
                completed_file = requested_destination.is_file() and (
                    requested_destination.stat().st_size == size
                )
                if status in ("awaiting_upload", "receiving") or (
                    status == "complete" and completed_file
                ):
                    state = self.store.update(resume_id)
                    state["resumed"] = True
                    return state
        destination = self.resolve_remote(requested, write=True, overwrite=overwrite)
        chunks = (size + self.config.chunk_size - 1) // self.config.chunk_size if size else 0
        state = self.store.create(
            {
                "direction": "upload",
                "status": "receiving" if pipeline else "awaiting_upload",
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
                "pipeline": pipeline,
                "inflight": inflight if pipeline else None,
                "chunks_staged": 0,
                "chunks_consumed": 0,
                "producer_complete": False,
                "ready_chunks": {},
                "compression": compression,
                "decoded_size": decoded_size if compression == "gzip" else None,
                "decoded_sha256": None,
                "metadata_ready": True,
                "resumable": resumable,
            }
        )
        if pipeline:
            self.start_worker(self._receive_upload_pipeline, str(state["id"]))
        state["resumed"] = False
        return state

    def create_download(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        requested = body.get("path")
        compression = body.get("compression")
        resumable = body.get("resume", False)
        resume_id = body.get("resume_id")
        pipeline = "inflight" in body
        inflight = body.get("inflight")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "path must be a string")
        if not isinstance(resumable, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "resume must be a boolean")
        if resume_id is not None and (
            not resumable
            or not isinstance(resume_id, str)
            or not TRANSFER_ID.fullmatch(resume_id)
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "resume_id must be a transfer id and requires resume",
            )
        if resumable and pipeline:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "resumable downloads cannot use the bounded pipeline",
            )
        if pipeline and (
            isinstance(inflight, bool)
            or not isinstance(inflight, int)
            or inflight <= 0
            or inflight > 128
        ):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "inflight must be an integer between 1 and 128",
            )
        if compression not in (None, "gzip"):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "compression must be gzip when provided",
            )
        if compression == "gzip" and not pipeline:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "compressed downloads require the pipeline protocol",
            )
        if resumable and compression is not None:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "resumable downloads cannot use compression",
            )
        source = self.resolve_remote(requested, write=False)
        stat = source.stat()
        if stat.st_size > self.config.max_file_size:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file_too_large", "file exceeds server max_file_size")
        wire_size = 0 if compression == "gzip" else stat.st_size
        chunks = (
            (wire_size + self.config.chunk_size - 1) // self.config.chunk_size
            if wire_size
            else 0
        )
        if resume_id is not None:
            try:
                previous = self.store.get(resume_id)
            except ApiError as exc:
                if exc.code != "not_found":
                    raise
                previous = None
            if previous is not None:
                matches = (
                    previous.get("direction") == "download"
                    and previous.get("resumable") is True
                    and previous.get("pipeline") is not True
                    and previous.get("compression") is None
                    and previous.get("path") == str(source)
                    and previous.get("size") == wire_size
                    and previous.get("mtime_ns") == stat.st_mtime_ns
                    and previous.get("source_dev") == stat.st_dev
                    and previous.get("source_ino") == stat.st_ino
                )
                if not matches:
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "resume_mismatch",
                        "saved download does not match the current NAS source",
                    )
                if previous.get("status") in ("preparing", "ready"):
                    state = self.store.update(resume_id)
                    state["resumed"] = True
                    return state
        state = self.store.create(
            {
                "direction": "download",
                "status": "preparing",
                "path": str(source),
                "size": wire_size,
                "mtime_ns": stat.st_mtime_ns,
                "source_dev": stat.st_dev,
                "source_ino": stat.st_ino,
                "chunk_size": self.config.chunk_size,
                "chunks": chunks,
                "bytes_transferred": 0,
                "sha256": None,
                "error": None,
                "objects_cleaned": chunks == 0,
                "pipeline": pipeline,
                "inflight": inflight if pipeline else None,
                "chunks_staged": 0,
                "chunks_consumed": 0,
                "producer_complete": False,
                "ready_chunks": {},
                "compression": compression,
                "decoded_size": stat.st_size if compression == "gzip" else None,
                "decoded_sha256": None,
                "metadata_ready": compression is None,
                "resumable": resumable,
            }
        )
        self.start_worker(self._prepare_download, str(state["id"]))
        state["resumed"] = False
        return state

    def urls(self, transfer_id: str, start: int, count: int) -> Dict[str, Any]:
        state = self.store.get(transfer_id)
        if state.get("resumable") is True:
            # URL requests are the resumable protocol's heartbeat. Without this,
            # a long but healthy copy could be removed by the TTL janitor.
            state = self.store.update(transfer_id)
        direction = state.get("direction")
        pipeline = state.get("pipeline") is True
        if direction == "upload":
            allowed = ("receiving",) if pipeline else ("awaiting_upload",)
            if state.get("status") not in allowed:
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "upload URLs are no longer available")
            method = "PUT"
        elif direction == "download":
            allowed = ("preparing", "ready") if pipeline else ("ready",)
            if state.get("status") not in allowed:
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "download is not ready")
            method = "GET"
        else:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "invalid_state", "invalid transfer direction")
        chunks = int(state["chunks"])
        if start < 0 or count <= 0 or count > 128 or start > chunks:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_range", "invalid URL batch range")
        end = min(start + count, chunks)
        ready_chunks = state.get("ready_chunks", {})
        if not isinstance(ready_chunks, dict):
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "invalid_state", "invalid chunk state")
        if pipeline and direction == "upload":
            consumed = int(state.get("chunks_consumed", 0))
            inflight = int(state.get("inflight", 0))
            if start < consumed or end > consumed + inflight:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "inflight_limit",
                    "requested upload URLs exceed the inflight window",
                )
        if pipeline and direction == "download":
            missing = [index for index in range(start, end) if str(index) not in ready_chunks]
            if missing:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "chunk_not_ready",
                    "download chunk %d is not ready" % missing[0],
                )
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
            item = {"index": index, "url": signed.url, "headers": signed.headers}
            if pipeline and direction == "download":
                item["sha256"] = ready_chunks[str(index)]
            items.append(item)
        return {"items": items, "next": end if end < chunks else None}

    def commit_upload(self, transfer_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        digest = body.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "sha256 must be lowercase hexadecimal")
        current = self.store.get(transfer_id)
        if current.get("direction") != "upload":
            raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "not an upload transfer")
        if current.get("pipeline") is True:
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                decoded_digest = body.get("decoded_sha256")
                if current.get("compression") == "gzip":
                    if not isinstance(decoded_digest, str) or not SHA256.fullmatch(
                        decoded_digest
                    ):
                        raise ApiError(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_request",
                            "decoded_sha256 must be lowercase hexadecimal",
                        )
                elif decoded_digest is not None:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        "decoded_sha256 requires gzip compression",
                    )
                if current.get("status") == "complete" or current.get("producer_complete"):
                    if current.get("sha256") != digest or current.get(
                        "decoded_sha256"
                    ) != decoded_digest:
                        raise ApiError(
                            HTTPStatus.CONFLICT,
                            "invalid_state",
                            "upload was committed with a different digest",
                        )
                    return current
                if current.get("status") != "receiving":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "invalid_state",
                        "upload is not receiving chunks",
                    )
                if int(current.get("chunks_staged", 0)) != int(current["chunks"]):
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "incomplete_upload",
                        "all chunks must be uploaded before commit",
                    )
                state = self.store.update(
                    transfer_id,
                    sha256=digest,
                    decoded_sha256=decoded_digest,
                    producer_complete=True,
                    error=None,
                )
                self._pipeline_condition.notify_all()
                return state
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

    def announce_upload_chunk(
        self, transfer_id: str, index: int, body: Mapping[str, Any]
    ) -> Dict[str, Any]:
        digest = body.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "sha256 must be lowercase hexadecimal",
            )
        with self._pipeline_condition:
            state = self.store.get(transfer_id)
            if state.get("direction") != "upload" or state.get("pipeline") is not True:
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "not a pipeline upload")
            if index < 0 or index >= int(state["chunks"]):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_range", "invalid chunk index")
            consumed = int(state.get("chunks_consumed", 0))
            if index < consumed:
                return state
            if state.get("status") != "receiving" or state.get("producer_complete"):
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "upload is no longer accepting chunks")
            if index >= consumed + int(state["inflight"]):
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "inflight_limit",
                    "chunk exceeds the inflight window",
                )
            ready = dict(state.get("ready_chunks", {}))
            key = str(index)
            if key in ready:
                if ready[key] != digest:
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "invalid_state",
                        "chunk was announced with a different digest",
                    )
                return state
            ready[key] = digest
            state = self.store.update(
                transfer_id,
                ready_chunks=ready,
                chunks_staged=int(state.get("chunks_staged", 0)) + 1,
            )
            self._pipeline_condition.notify_all()
            return state

    def acknowledge_download_chunk(
        self, transfer_id: str, index: int, body: Mapping[str, Any]
    ) -> Dict[str, Any]:
        digest = body.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "sha256 must be lowercase hexadecimal",
            )
        with self._pipeline_condition:
            state = self.store.get(transfer_id)
            if state.get("direction") != "download" or state.get("pipeline") is not True:
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "not a pipeline download")
            consumed = int(state.get("chunks_consumed", 0))
            if index < consumed:
                return state
            if index != consumed:
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "out_of_order",
                    "download chunks must be acknowledged in order",
                )
            if state.get("status") not in ("preparing", "ready"):
                raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "download is not active")
            ready = dict(state.get("ready_chunks", {}))
            expected_digest = ready.get(str(index))
            if expected_digest is None:
                raise ApiError(HTTPStatus.CONFLICT, "chunk_not_ready", "download chunk is not ready")
            if digest != expected_digest:
                raise ApiError(HTTPStatus.CONFLICT, "digest_mismatch", "download chunk digest differs")
            self._retry(
                lambda: self.s3.delete_chunk(transfer_id, index),
                "delete chunk %d" % index,
            )
            ready.pop(str(index), None)
            consumed += 1
            state = self.store.update(
                transfer_id,
                ready_chunks=ready,
                chunks_consumed=consumed,
                objects_cleaned=consumed == int(state["chunks"]),
            )
            self._pipeline_condition.notify_all()
            return state

    def acknowledge_download(self, transfer_id: str) -> Dict[str, Any]:
        state = self.store.get(transfer_id)
        if state.get("direction") != "download":
            raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "not a download transfer")
        if state.get("pipeline") is True:
            with self._pipeline_condition:
                state = self.store.get(transfer_id)
                if state.get("status") == "complete":
                    return state
                if state.get("status") != "ready" or not state.get("producer_complete"):
                    raise ApiError(HTTPStatus.CONFLICT, "invalid_state", "download is not ready")
                if int(state.get("chunks_consumed", 0)) != int(state["chunks"]):
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "incomplete_download",
                        "all chunks must be acknowledged before completion",
                    )
                state = self.store.update(
                    transfer_id,
                    status="complete",
                    error=None,
                    objects_cleaned=True,
                )
                self._pipeline_condition.notify_all()
                return state
        if state.get("status") in ("cleaning", "complete"):
            return state
        state = self.store.transition(transfer_id, ("ready",), "cleaning")
        self.start_worker(self._cleanup_complete, transfer_id)
        return state

    def abort(self, transfer_id: str) -> Dict[str, Any]:
        current = self.store.get(transfer_id)
        if current.get("pipeline") is True and current.get("status") in (
            "receiving",
            "preparing",
        ):
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                if current.get("status") in ("receiving", "preparing"):
                    state = self.store.update(
                        transfer_id,
                        status="error",
                        error="transfer aborted by client",
                    )
                    self._pipeline_condition.notify_all()
                    return state
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

    def _pipeline_expired(self, state: Mapping[str, Any]) -> bool:
        return time.time() - float(state.get("updated_at", 0)) >= self.config.transfer_ttl_seconds

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

    def _download_compressed_path(self, state: Mapping[str, Any]) -> Path:
        return self.config.state_dir / "compressed" / ("%s.gz" % state["id"])

    def _remove_download_temporary(self, state: Mapping[str, Any]) -> None:
        if state.get("direction") != "download":
            return
        try:
            self._download_compressed_path(state).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("could not remove compressed NAS file for %s: %s", state["id"], exc)

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
            cleaned = self._cleanup_state_objects(state)
            self.store.update(transfer_id, objects_cleaned=cleaned)
            LOG.info("upload %s completed", transfer_id)
        except Exception as exc:
            LOG.error("upload %s failed: %s", transfer_id, exc)
            self.store.update(transfer_id, status="error", error=str(exc))
            cleaned = self._cleanup_state_objects(self.store.get(transfer_id))
            self.store.update(transfer_id, objects_cleaned=cleaned)
        finally:
            self._remove_upload_temporary(state)

    def _receive_upload_pipeline(self, transfer_id: str) -> None:
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
            transferred = 0
            with os.fdopen(fd, "wb") as handle:
                decoded_size = (
                    int(state["decoded_size"])
                    if state.get("compression") == "gzip"
                    else int(state["size"])
                )
                sink = DecodingWriter(handle, state.get("compression"), decoded_size)
                try:
                    os.chmod(str(temporary), 0o600)
                except OSError:
                    pass
                for index in range(int(state["chunks"])):
                    with self._pipeline_condition:
                        while True:
                            current = self.store.get(transfer_id)
                            if current.get("status") != "receiving":
                                raise Nass3cpError(
                                    str(current.get("error") or "upload was cancelled")
                                )
                            if self._pipeline_expired(current):
                                raise Nass3cpError("upload pipeline timed out waiting for a chunk")
                            ready = current.get("ready_chunks", {})
                            chunk_digest = ready.get(str(index)) if isinstance(ready, dict) else None
                            if isinstance(chunk_digest, str):
                                break
                            if current.get("producer_complete"):
                                raise Nass3cpError("upload committed before all chunks were ready")
                            self._pipeline_condition.wait(timeout=1.0)

                    expected = min(
                        int(state["chunk_size"]),
                        int(state["size"]) - transferred,
                    )
                    data = self._get_chunk_bytes(transfer_id, index, expected)
                    if hashlib.sha256(data).hexdigest() != chunk_digest:
                        raise Nass3cpError("chunk %d SHA-256 mismatch" % index)
                    sink.write(data)
                    self._retry(
                        lambda current=index: self.s3.delete_chunk(transfer_id, current),
                        "delete chunk %d" % index,
                    )
                    transferred += len(data)

                    with self._pipeline_condition:
                        current = self.store.get(transfer_id)
                        if current.get("status") != "receiving":
                            raise Nass3cpError(
                                str(current.get("error") or "upload was cancelled")
                            )
                        ready = dict(current.get("ready_chunks", {}))
                        if ready.get(str(index)) != chunk_digest:
                            raise Nass3cpError("chunk state changed while it was being received")
                        ready.pop(str(index), None)
                        self.store.update(
                            transfer_id,
                            ready_chunks=ready,
                            chunks_consumed=index + 1,
                            bytes_transferred=transferred,
                            objects_cleaned=index + 1 == int(state["chunks"]),
                        )
                        self._pipeline_condition.notify_all()

                wire_digest, decoded_digest = sink.finish()
                with self._pipeline_condition:
                    while True:
                        current = self.store.get(transfer_id)
                        if current.get("status") != "receiving":
                            raise Nass3cpError(
                                str(current.get("error") or "upload was cancelled")
                            )
                        if self._pipeline_expired(current):
                            raise Nass3cpError("upload pipeline timed out waiting for commit")
                        if current.get("producer_complete"):
                            break
                        self._pipeline_condition.wait(timeout=1.0)
                handle.flush()
                os.fsync(handle.fileno())

            if wire_digest != current.get("sha256"):
                raise Nass3cpError("end-to-end SHA-256 mismatch")
            if (
                state.get("compression") == "gzip"
                and decoded_digest != current.get("decoded_sha256")
            ):
                raise Nass3cpError("decoded end-to-end SHA-256 mismatch")
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                if current.get("status") != "receiving":
                    raise Nass3cpError(str(current.get("error") or "upload was cancelled"))
                self._revalidate_upload_destination(destination)
                if destination.exists() and not state.get("overwrite"):
                    raise Nass3cpError(
                        "destination appeared during transfer; refusing to overwrite it"
                    )
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
                    objects_cleaned=True,
                )
                self._pipeline_condition.notify_all()
            LOG.info("pipeline upload %s completed", transfer_id)
        except Exception as exc:
            LOG.error("pipeline upload %s failed: %s", transfer_id, exc)
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                message = str(current.get("error") or exc)
                self.store.update(transfer_id, status="error", error=message)
                self._pipeline_condition.notify_all()
            cleaned = self._cleanup_state_objects(self.store.get(transfer_id))
            self.store.update(transfer_id, objects_cleaned=cleaned)
        finally:
            self._remove_upload_temporary(state)

    def _prepare_download(self, transfer_id: str) -> None:
        state = self.store.get(transfer_id)
        if state.get("compression") == "gzip":
            self._prepare_download_compressed(transfer_id)
            return
        if state.get("pipeline") is True:
            self._prepare_download_pipeline(transfer_id)
            return
        source = Path(str(state["path"]))
        try:
            digest = hashlib.sha256()
            transferred = 0
            with source.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if (
                    before.st_dev != state["source_dev"]
                    or before.st_ino != state["source_ino"]
                    or before.st_size != state["size"]
                    or before.st_mtime_ns != state["mtime_ns"]
                ):
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

    def _prepare_download_compressed(self, transfer_id: str) -> None:
        state = self.store.get(transfer_id)
        source = Path(str(state["path"]))
        compressed = self._download_compressed_path(state)
        try:
            compressed.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(str(compressed.parent), 0o700)
            except OSError:
                pass
            fd = os.open(
                str(compressed),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            last_reported = 0

            def report(processed: int) -> None:
                nonlocal last_reported
                if processed - last_reported < int(state["chunk_size"]):
                    return
                current = self.store.get(transfer_id)
                if current.get("status") != "preparing":
                    raise Nass3cpError(str(current.get("error") or "download was cancelled"))
                self.store.update(transfer_id, bytes_transferred=processed)
                last_reported = processed

            with os.fdopen(fd, "wb") as output, source.open("rb") as source_handle:
                before = os.fstat(source_handle.fileno())
                if (
                    before.st_dev != state["source_dev"]
                    or before.st_ino != state["source_ino"]
                    or before.st_size != state["decoded_size"]
                    or before.st_mtime_ns != state["mtime_ns"]
                ):
                    raise Nass3cpError("source file was replaced before compression")
                decoded_digest, decoded_size = gzip_compress_stream(
                    source_handle,
                    output,
                    report,
                )
                output.flush()
                os.fsync(output.fileno())
                after = os.fstat(source_handle.fileno())
                if (
                    after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                    or decoded_size != int(state["decoded_size"])
                ):
                    raise Nass3cpError("source file changed during compression")

            compressed_size = compressed.stat().st_size
            if compressed_size >= decoded_size:
                self._remove_download_temporary(state)
                wire_size = decoded_size
                compression = None
                decoded_state_size = None
                decoded_state_digest = None
                pipeline_source = source
                verify_identity = True
            else:
                wire_size = compressed_size
                compression = "gzip"
                decoded_state_size = decoded_size
                decoded_state_digest = decoded_digest
                pipeline_source = compressed
                verify_identity = False
            chunks = (
                (wire_size + int(state["chunk_size"]) - 1) // int(state["chunk_size"])
                if wire_size
                else 0
            )
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                if current.get("status") != "preparing":
                    raise Nass3cpError(str(current.get("error") or "download was cancelled"))
                self.store.update(
                    transfer_id,
                    size=wire_size,
                    chunks=chunks,
                    bytes_transferred=0,
                    compression=compression,
                    decoded_size=decoded_state_size,
                    decoded_sha256=decoded_state_digest,
                    metadata_ready=True,
                    objects_cleaned=chunks == 0,
                )
                self._pipeline_condition.notify_all()
            self._prepare_download_pipeline(
                transfer_id,
                source=pipeline_source,
                verify_identity=verify_identity,
            )
        except Exception as exc:
            LOG.error("download compression %s failed: %s", transfer_id, exc)
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                message = str(current.get("error") or exc)
                self.store.update(transfer_id, status="error", error=message)
                self._pipeline_condition.notify_all()
            cleaned = self._cleanup_state_objects(self.store.get(transfer_id))
            self.store.update(transfer_id, objects_cleaned=cleaned)
        finally:
            self._remove_download_temporary(state)

    def _prepare_download_pipeline(
        self,
        transfer_id: str,
        source: Optional[Path] = None,
        verify_identity: bool = True,
    ) -> None:
        state = self.store.get(transfer_id)
        pipeline_source = source if source is not None else Path(str(state["path"]))
        try:
            digest = hashlib.sha256()
            transferred = 0
            with pipeline_source.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if verify_identity and (
                    before.st_dev != state["source_dev"]
                    or before.st_ino != state["source_ino"]
                    or before.st_size != state["size"]
                    or before.st_mtime_ns != state["mtime_ns"]
                ):
                    raise Nass3cpError("source file was replaced before it could be read")
                for index in range(int(state["chunks"])):
                    with self._pipeline_condition:
                        while True:
                            current = self.store.get(transfer_id)
                            if current.get("status") != "preparing":
                                raise Nass3cpError(
                                    str(current.get("error") or "download was cancelled")
                                )
                            if self._pipeline_expired(current):
                                raise Nass3cpError(
                                    "download pipeline timed out waiting for chunk acknowledgement"
                                )
                            outstanding = int(current.get("chunks_staged", 0)) - int(
                                current.get("chunks_consumed", 0)
                            )
                            if outstanding < int(state["inflight"]):
                                break
                            self._pipeline_condition.wait(timeout=1.0)

                    data = handle.read(int(state["chunk_size"]))
                    if not data:
                        raise Nass3cpError("source file became shorter during transfer")
                    chunk_digest = hashlib.sha256(data).hexdigest()
                    self._retry(
                        lambda current=index, payload=data: self.s3.put_chunk(
                            transfer_id, current, payload
                        ),
                        "upload chunk %d" % index,
                    )
                    digest.update(data)
                    transferred += len(data)

                    with self._pipeline_condition:
                        current = self.store.get(transfer_id)
                        if current.get("status") != "preparing":
                            raise Nass3cpError(
                                str(current.get("error") or "download was cancelled")
                            )
                        ready = dict(current.get("ready_chunks", {}))
                        ready[str(index)] = chunk_digest
                        self.store.update(
                            transfer_id,
                            ready_chunks=ready,
                            chunks_staged=index + 1,
                            bytes_transferred=transferred,
                        )
                        self._pipeline_condition.notify_all()

                if handle.read(1):
                    raise Nass3cpError("source file grew during transfer")
                after = os.fstat(handle.fileno())
                if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                    raise Nass3cpError("source file changed during transfer")

            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                if current.get("status") != "preparing":
                    raise Nass3cpError(str(current.get("error") or "download was cancelled"))
                self.store.update(
                    transfer_id,
                    status="ready",
                    bytes_transferred=transferred,
                    sha256=digest.hexdigest(),
                    producer_complete=True,
                    error=None,
                )
                self._pipeline_condition.notify_all()
            LOG.info("pipeline download %s is ready", transfer_id)
        except Exception as exc:
            LOG.error("pipeline download preparation %s failed: %s", transfer_id, exc)
            with self._pipeline_condition:
                current = self.store.get(transfer_id)
                message = str(current.get("error") or exc)
                self.store.update(transfer_id, status="error", error=message)
                self._pipeline_condition.notify_all()
            cleaned = self._cleanup_state_objects(self.store.get(transfer_id))
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

    def _cleanup_state_objects(self, state: Mapping[str, Any]) -> bool:
        transfer_id = str(state["id"])
        chunks = int(state["chunks"])
        if state.get("pipeline") is not True:
            return self._cleanup_objects(transfer_id, chunks)
        try:
            start = int(state.get("chunks_consumed", 0))
            inflight = int(state.get("inflight", 0))
        except (TypeError, ValueError):
            return self._cleanup_objects(transfer_id, chunks)
        if start < 0 or start > chunks or inflight <= 0:
            return self._cleanup_objects(transfer_id, chunks)
        errors = []
        for index in range(start, min(chunks, start + inflight)):
            try:
                self._retry(
                    lambda current=index: self.s3.delete_chunk(transfer_id, current),
                    "delete chunk %d" % index,
                )
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            LOG.warning(
                "could not clean %d pipeline object(s) for %s: %s",
                len(errors),
                transfer_id,
                errors[0],
            )
            return False
        return True

    def _cleanup_complete(self, transfer_id: str) -> None:
        state = self.store.get(transfer_id)
        cleaned = self._cleanup_state_objects(state)
        self.store.update(
            transfer_id, status="complete", error=None, objects_cleaned=cleaned
        )

    def _cleanup_error(self, transfer_id: str, message: str) -> None:
        state = self.store.get(transfer_id)
        cleaned = self._cleanup_state_objects(state)
        self.store.update(
            transfer_id, status="error", error=message, objects_cleaned=cleaned
        )

    def cleanup_interrupted(self) -> None:
        for state in self.store.all():
            if state.get("status") not in ("complete", "error"):
                continue
            transfer_id = str(state["id"])
            self._remove_upload_temporary(state)
            self._remove_download_temporary(state)
            if state.get("objects_cleaned"):
                continue
            cleaned = self._cleanup_state_objects(state)
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
                self._remove_download_temporary(state)
                if not state.get("objects_cleaned"):
                    self._cleanup_state_objects(state)
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
        # ASCII escaping also makes Unix filenames containing surrogate-escaped bytes safe to return.
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
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
            {"error": {"code": "unauthorized", "message": "valid password required"}},
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
            if path == "/v1/list":
                self._send_json(HTTPStatus.OK, self.app.list_directory(body))
                return
            if path == "/v1/path-info":
                self._send_json(HTTPStatus.OK, self.app.path_info(body))
                return
            if path == "/v1/directories":
                result = self.app.ensure_directory(body)
                self._send_json(
                    HTTPStatus.CREATED if result["created"] else HTTPStatus.OK,
                    result,
                )
                return
            if path == "/v1/transfers/upload":
                state = self.app.create_upload(body)
                self._send_json(HTTPStatus.CREATED, _public_state(state))
                return
            if path == "/v1/transfers/download":
                state = self.app.create_download(body)
                self._send_json(HTTPStatus.ACCEPTED, _public_state(state))
                return
            chunk_match = re.fullmatch(
                r"/v1/transfers/([0-9a-f]{32})/chunks/([0-9]{1,10})/(ready|ack)",
                path,
            )
            if chunk_match:
                transfer_id, raw_index, action = chunk_match.groups()
                index = int(raw_index)
                if action == "ready":
                    state = self.app.announce_upload_chunk(transfer_id, index, body)
                else:
                    state = self.app.acknowledge_download_chunk(transfer_id, index, body)
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
    parser.add_argument(
        "--env-file",
        help="load KEY=VALUE secrets from this file before reading the configuration",
    )
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


def validate_server_config(config: ServerConfig) -> Optional[ssl.SSLContext]:
    for root in config.allowed_roots:
        if not root.is_dir():
            raise ConfigError("allowed root is not a directory: %s" % root)
    if not config.tls_enabled:
        return None
    if config.cert_file is None or not config.cert_file.is_file():
        raise ConfigError("TLS certificate does not exist: %s" % config.cert_file)
    if config.key_file is None or not config.key_file.is_file():
        raise ConfigError("TLS private key does not exist: %s" % config.key_file)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(config.cert_file), str(config.key_file))
    return context


def run(config: ServerConfig) -> None:
    context = validate_server_config(config)
    app = ServerApp(config)
    server = Nass3cpHTTPServer((config.listen, config.port), RequestHandler, app)
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    janitor = threading.Thread(target=app.janitor, daemon=True)
    janitor.start()
    app.start_worker(app.cleanup_interrupted)
    if context is None:
        LOG.warning(
            "listening without application TLS on %s:%d; the overlay/tunnel must be encrypted",
            config.listen,
            config.port,
        )
    else:
        LOG.info("listening with TLS on %s:%d", config.listen, config.port)
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.stop()
        server.server_close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.env_file:
            load_environment_file(args.env_file)
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
