"""Bounded, cancellable filename searches in an isolated worker process."""

import multiprocessing
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .config import SearchConfig
from .errors import Nass3cpError
from .metadata import birthtime_ns


ACTIVE_STATUSES = ("starting", "running", "cancelling")
MAX_PATTERN_LENGTH = 512
MAX_DEPTH = 128


class SearchError(Nass3cpError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _Pacer:
    """One operation per interval, without bursts after a slow disk operation."""

    def __init__(self, rate: int, clock: Callable = time.monotonic, sleep: Callable = time.sleep):
        self.interval = 1.0 / rate
        self.clock, self.sleep = clock, sleep
        self.next_at = clock()

    def wait(self) -> None:
        delay = self.next_at - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.next_at = self.clock() + self.interval


def _counters(path: str) -> Dict[str, Any]:
    return {
        "scanned_entries": 0, "scanned_files": 0, "directories_discovered": 1,
        "directories_completed": 0, "completed_directory_entries": 0,
        "skipped_entries": 0, "current_path": path,
    }


def _is_link(details: os.stat_result) -> bool:
    # Windows junctions are reparse points but are not always reported as symlinks.
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & 0x400)


def _search_worker(connection: Any, root_name: str, pattern: str, regex: bool,
                   case_sensitive: bool, settings: SearchConfig) -> None:
    root = Path(root_name)
    stats = _counters(root.as_posix())
    stack = []
    pacer = _Pacer(settings.entries_per_second)
    last_update = 0.0
    found = 0

    def emit(phase: str = "scan", **values: Any) -> None:
        nonlocal last_update
        connection.send(dict(values, phase=phase, stats=dict(stats)))
        last_update = time.monotonic()

    def open_directory(directory: Path) -> None:
        pacer.wait()
        details = directory.lstat()
        directory.relative_to(root)
        if _is_link(details) or directory.resolve() != directory or not stat.S_ISDIR(details.st_mode):
            raise OSError("directory changed or is a symbolic link")
        stack.append([directory, os.scandir(str(directory)), 0])

    try:
        if hasattr(os, "nice"):
            try:
                os.nice(10)
            except OSError:
                pass
        matcher = None
        if regex:
            emit("regex")
            matcher = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
            emit()
        needle = pattern if case_sensitive else pattern.casefold()
        open_directory(root)
        emit()
        while stack:
            directory, iterator, _ = stack[-1]
            stats["current_path"] = directory.as_posix()
            if time.monotonic() - last_update >= 0.25:
                emit()
            pacer.wait()
            try:
                entry = next(iterator)
            except StopIteration:
                stats["directories_completed"] += 1
                stats["completed_directory_entries"] += stack[-1][2]
                iterator.close()
                stack.pop()
                emit()
                continue
            except OSError:
                stats["skipped_entries"] += 1
                iterator.close()
                stack.pop()
                continue
            stats["scanned_entries"] += 1
            stack[-1][2] += 1
            try:
                if entry.is_symlink():
                    stats["skipped_entries"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if len(stack) >= MAX_DEPTH:
                        stats["skipped_entries"] += 1
                        continue
                    open_directory(Path(entry.path))
                    stats["directories_discovered"] += 1
                    continue
                if not entry.is_file(follow_symlinks=False):
                    stats["skipped_entries"] += 1
                    continue
                stats["scanned_files"] += 1
                if matcher is not None:
                    emit("regex")
                    matched = matcher.search(entry.name) is not None
                    emit()
                else:
                    matched = needle in (entry.name if case_sensitive else entry.name.casefold())
                if not matched:
                    continue
                details = entry.stat(follow_symlinks=False)
                candidate = Path(entry.path)
                if _is_link(details) or not stat.S_ISREG(details.st_mode) or candidate.resolve() != candidate:
                    stats["skipped_entries"] += 1
                    continue
                candidate.relative_to(root)
                found += 1
                emit(result={
                    "name": entry.name, "path": candidate.as_posix(),
                    "parent_path": directory.as_posix(),
                    "relative_path": candidate.relative_to(root).as_posix(), "type": "file",
                    "size": details.st_size, "mtime_ns": details.st_mtime_ns,
                    "birthtime_ns": birthtime_ns(details),
                })
                if found >= settings.max_results:
                    emit(status="limited", error="Result limit reached. Narrow the search to find more files.")
                    return
            except (OSError, ValueError):
                stats["skipped_entries"] += 1
        stats["current_path"] = ""
        emit(status="completed")
    except (re.error, RecursionError, OverflowError) as exc:
        emit(status="failed", error="Invalid regular expression: %s" % exc)
    except Exception as exc:
        try:
            emit(status="failed", error="Search failed: %s" % exc)
        except (OSError, EOFError):
            pass
    finally:
        for _, iterator, _ in stack:
            iterator.close()
        connection.close()


class _Job:
    def __init__(self, path: Path, pattern: str, regex: bool, case_sensitive: bool):
        self.id = secrets.token_hex(16)
        self.path = path.as_posix()
        self.pattern, self.regex, self.case_sensitive = pattern, regex, case_sensitive
        self.status = "starting"
        self.error: Optional[str] = None
        self.stats = _counters(self.path)
        self.results = []  # type: list
        self.started_at = self.last_poll = time.monotonic()
        self.finished_at: Optional[float] = None
        self.cancelled = threading.Event()
        self.thread: Optional[threading.Thread] = None


class SearchManager:
    """Allow one NAS-wide scan; polling reads memory, never rescans the disk."""

    def __init__(self, settings: SearchConfig, resolve_directory: Callable[[str], Path],
                 idle_timeout: float = 60, retention: float = 600):
        self.settings, self.resolve_directory = settings, resolve_directory
        self.idle_timeout, self.retention = idle_timeout, retention
        self._lock = threading.RLock()
        self._jobs: Dict[str, _Job] = {}
        self._active: Optional[str] = None
        self._closed = False

    def start(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        path, pattern = body.get("path"), body.get("pattern")
        regex, case_sensitive = body.get("regex", False), body.get("case_sensitive", False)
        if not isinstance(path, str) or "\x00" in path:
            raise SearchError(400, "invalid_request", "path must be a NAS directory")
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_PATTERN_LENGTH or "\x00" in pattern:
            raise SearchError(400, "invalid_pattern", "pattern must contain 1 to 512 characters")
        if not isinstance(regex, bool) or not isinstance(case_sensitive, bool):
            raise SearchError(400, "invalid_request", "regex and case_sensitive must be booleans")
        with self._lock:
            if self._closed:
                raise SearchError(503, "search_unavailable", "NAS service is shutting down")
            if self._active is not None:
                raise SearchError(409, "search_busy", "A search is already running on this NAS. Cancel it or wait for it to finish.")
            directory = self.resolve_directory(path or ".")
            self._prune()
            job = _Job(directory, pattern, regex, case_sensitive)
            context = multiprocessing.get_context("spawn")
            reader, writer = context.Pipe(duplex=False)
            process = context.Process(
                target=_search_worker,
                args=(writer, str(directory), pattern, regex, case_sensitive, self.settings),
                daemon=True,
            )
            try:
                process.start()
            except Exception:
                reader.close()
                writer.close()
                raise
            writer.close()
            self._jobs[job.id] = job
            self._active = job.id
            job.thread = threading.Thread(target=self._monitor, args=(job, process, reader), daemon=True)
            job.thread.start()
            return self._snapshot(job, 0, 100)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.retention
        for identifier, job in list(self._jobs.items()):
            if job.finished_at is not None and job.finished_at < cutoff:
                del self._jobs[identifier]
        # Retain at most four finished searches, regardless of the TTL.
        while len(self._jobs) >= 4:
            del self._jobs[next(iter(self._jobs))]

    def _monitor(self, job: _Job, process: Any, reader: Any) -> None:
        regex_started: Optional[float] = None
        status, error = "failed", "Search worker exited unexpectedly."
        try:
            while True:
                now = time.monotonic()
                with self._lock:
                    idle = now - job.last_poll > self.idle_timeout
                if job.cancelled.is_set() or idle:
                    status = "cancelled"
                    error = "Search stopped because no browser is polling its progress." if idle else None
                    break
                if now - job.started_at > 24 * 3600:
                    error = "Search exceeded the 24-hour time limit. Narrow the search folder."
                    break
                if reader.poll(0.02):
                    message = reader.recv()
                    with self._lock:
                        job.status = "running"
                        job.stats = message["stats"]
                        if "result" in message:
                            job.results.append(message["result"])
                    regex_started = time.monotonic() if message["phase"] == "regex" else None
                    if "status" in message:
                        status, error = message["status"], message.get("error")
                        break
                elif regex_started is not None and time.monotonic() - regex_started > self.settings.regex_timeout_ms / 1000:
                    error = "Regular expression exceeded the %d ms time limit. Simplify the pattern." % self.settings.regex_timeout_ms
                    break
                elif not process.is_alive():
                    break
                elif job.status == "starting" and now - job.started_at > 30:
                    error = "Search worker could not start within 30 seconds."
                    break
        except (OSError, EOFError):
            pass
        finally:
            # Scanning is read-only, so terminating also safely interrupts slow regexes or I/O.
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
            while process.is_alive():
                process.join(timeout=0.1)
            reader.close()
            process.close()
            with self._lock:
                job.status, job.error = status, error
                job.finished_at = time.monotonic()
                self._active = None

    def _snapshot(self, job: _Job, cursor: int, limit: int) -> Dict[str, Any]:
        stats = dict(job.stats)
        estimate = None
        if job.status == "completed":
            estimate = 100
        elif stats["directories_completed"]:
            # Include partially scanned folders, so finishing a tiny folder first
            # does not make a later large folder appear almost complete.
            average = max(
                stats["completed_directory_entries"] / stats["directories_completed"],
                stats["scanned_entries"] / stats["directories_discovered"],
            )
            pending = max(1, stats["directories_discovered"] - stats["directories_completed"])
            remaining = pending * max(1, average)
            estimate = min(99, int(100 * stats["scanned_entries"] / (stats["scanned_entries"] + remaining)))
        end = min(len(job.results), cursor + limit)
        stats.update({
            "id": job.id, "status": job.status, "error": job.error, "path": job.path,
            "pattern": job.pattern, "regex": job.regex, "case_sensitive": job.case_sensitive,
            "estimated_percent": estimate,
            "elapsed_seconds": round((job.finished_at or time.monotonic()) - job.started_at, 1),
            "rate_limit": self.settings.entries_per_second, "max_results": self.settings.max_results,
            "results_count": len(job.results), "results": job.results[cursor:end],
            "next_cursor": end if end < len(job.results) else None,
        })
        return stats

    def state(self, identifier: str, cursor: int = 0, limit: int = 100) -> Dict[str, Any]:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise SearchError(400, "invalid_request", "cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SearchError(400, "invalid_request", "limit must be between 1 and 100")
        with self._lock:
            job = self._jobs.get(identifier)
            if job is None or (job.finished_at is not None and time.monotonic() - job.finished_at > self.retention):
                raise SearchError(404, "search_not_found", "Search expired or does not exist. Start a new search.")
            job.last_poll = time.monotonic()
            return self._snapshot(job, cursor, limit)

    def cancel(self, identifier: str) -> Dict[str, Any]:
        with self._lock:
            self.state(identifier)
            job = self._jobs[identifier]
            if job.status in ACTIVE_STATUSES:
                job.status = "cancelling"
                job.cancelled.set()
            return self._snapshot(job, 0, 100)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancelled.set()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(timeout=2)
