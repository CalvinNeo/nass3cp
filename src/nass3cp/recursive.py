import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from .client import ApiClient, _ProgressDisplay, download, list_remote, upload
from .compression import gzip_compress_stream, should_compress
from .errors import Nass3cpError


_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % number for number in range(1, 10)}
    | {"LPT%d" % number for number in range(1, 10)}
)


@dataclass(frozen=True)
class TreeFile:
    relative: str
    size: int
    mtime_ns: int
    local_path: Optional[Path] = None
    device: Optional[int] = None
    inode: Optional[int] = None


@dataclass
class TreeSnapshot:
    root_exists: bool
    root_type: Optional[str]
    directories: List[str]
    files: List[TreeFile]
    entry_types: Dict[str, str]
    unsupported: List[str]


@dataclass
class RecursivePlan:
    source: TreeSnapshot
    transfers: List[TreeFile]
    skipped: List[TreeFile]
    directories_to_create: List[str]
    conflicts: List[str]
    policy: str

    @property
    def original_size(self) -> int:
        return sum(item.size for item in self.source.files)

    @property
    def transfer_size(self) -> int:
        return sum(item.size for item in self.transfers)

    @property
    def skipped_size(self) -> int:
        return sum(item.size for item in self.skipped)

    @property
    def compression_candidates(self) -> List[TreeFile]:
        if self.policy != "auto":
            return []
        return [item for item in self.transfers if should_compress(item.relative)]


def _human_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return "%d B" % int(amount) if unit == "B" else "%.1f %s" % (amount, unit)
        amount /= 1024.0
    return "%.1f PiB" % amount


def _relative_child(parent: str, name: str) -> str:
    return name if not parent else parent + "/" + name


def _remote_child(root: str, relative: str) -> str:
    if not relative:
        return root
    if root.endswith(("/", "\\")):
        return root + relative
    if root == ".":
        return "./" + relative
    return root + "/" + relative


def _local_child(root: Path, relative: str) -> Path:
    parts = relative.split("/") if relative else []
    if any(not part or part in (".", "..") for part in parts):
        raise Nass3cpError("unsafe relative path in recursive copy: %r" % relative)
    if os.name == "nt":
        for part in parts:
            invalid_character = any(
                ord(character) < 32 or character in '<>:"\\|?*' for character in part
            )
            reserved = part.rstrip(" .").split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            if invalid_character or part.endswith((" ", ".")) or reserved:
                raise Nass3cpError(
                    "NAS path cannot be represented safely on Windows: %r" % relative
                )
    return root.joinpath(*parts)


def _mode_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _local_type(path: Path) -> Optional[str]:
    try:
        return _mode_type(path.lstat().st_mode)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return "blocked"


def scan_local_tree(root_value: str) -> TreeSnapshot:
    root = Path(root_value)
    try:
        root_details = os.stat(str(root), follow_symlinks=False)
        root_type = _mode_type(root_details.st_mode)
    except FileNotFoundError:
        root_details = None
        root_type = None
    except OSError as exc:
        raise Nass3cpError("cannot inspect local source directory %s: %s" % (root, exc)) from exc
    if root_type != "directory":
        if root_type is None:
            raise Nass3cpError("local source directory does not exist: %s" % root)
        raise Nass3cpError("local recursive source is not a directory: %s" % root)

    directories: List[str] = []
    files: List[TreeFile] = []
    entry_types: Dict[str, str] = {}
    unsupported: List[str] = []
    if root_details is None:
        raise Nass3cpError("local source directory does not exist: %s" % root)
    pending: List[Tuple[Path, str, int, int]] = [
        (root, "", root_details.st_dev, root_details.st_ino)
    ]
    while pending:
        directory, relative_directory, expected_device, expected_inode = pending.pop()
        try:
            current_directory = os.stat(str(directory), follow_symlinks=False)
            if (
                not stat.S_ISDIR(current_directory.st_mode)
                or current_directory.st_dev != expected_device
                or current_directory.st_ino != expected_inode
            ):
                raise Nass3cpError(
                    "local directory changed during recursive scan: %s" % directory
                )
            with os.scandir(str(directory)) as iterator:
                entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
        except Nass3cpError:
            raise
        except OSError as exc:
            raise Nass3cpError("cannot scan local directory %s: %s" % (directory, exc)) from exc
        for entry in entries:
            relative = _relative_child(relative_directory, entry.name)
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise Nass3cpError("cannot inspect local path %s: %s" % (entry.path, exc)) from exc
            kind = _mode_type(details.st_mode)
            entry_types[relative] = kind
            if kind == "directory":
                try:
                    details = os.stat(entry.path, follow_symlinks=False)
                except OSError as exc:
                    raise Nass3cpError(
                        "cannot inspect local directory %s: %s" % (entry.path, exc)
                    ) from exc
                kind = _mode_type(details.st_mode)
                entry_types[relative] = kind
                if kind != "directory":
                    unsupported.append(relative)
                    continue
                directories.append(relative)
                pending.append((Path(entry.path), relative, details.st_dev, details.st_ino))
            elif kind == "file":
                try:
                    # On Windows DirEntry.stat() may use cached directory metadata
                    # whose st_dev/st_ino are both zero. A direct non-following stat
                    # gives the identity later compared with the opened file handle.
                    details = os.stat(entry.path, follow_symlinks=False)
                except OSError as exc:
                    raise Nass3cpError(
                        "cannot inspect local file %s: %s" % (entry.path, exc)
                    ) from exc
                if not stat.S_ISREG(details.st_mode):
                    entry_types[relative] = _mode_type(details.st_mode)
                    unsupported.append(relative)
                    continue
                files.append(
                    TreeFile(
                        relative=relative,
                        size=details.st_size,
                        mtime_ns=details.st_mtime_ns,
                        local_path=Path(entry.path),
                        device=details.st_dev,
                        inode=details.st_ino,
                    )
                )
            else:
                unsupported.append(relative)
    directories.sort(key=lambda value: (value.count("/"), value.casefold(), value))
    files.sort(key=lambda item: (item.relative.casefold(), item.relative))
    unsupported.sort(key=lambda value: (value.casefold(), value))
    return TreeSnapshot(True, "directory", directories, files, entry_types, unsupported)


def scan_remote_tree(
    api: ApiClient,
    root: str,
    missing_ok: bool = False,
    non_directory_ok: bool = False,
) -> TreeSnapshot:
    root_info = api.path_info(root)
    if not root_info["exists"]:
        if missing_ok:
            return TreeSnapshot(False, None, [], [], {}, [])
        raise Nass3cpError("NAS source directory does not exist: %s" % root)
    if root_info.get("type") != "directory":
        if non_directory_ok:
            return TreeSnapshot(True, str(root_info.get("type")), [], [], {}, [])
        raise Nass3cpError("NAS recursive path is not a directory: %s" % root)

    directories: List[str] = []
    files: List[TreeFile] = []
    entry_types: Dict[str, str] = {}
    unsupported: List[str] = []
    pending: List[Tuple[str, str]] = [(root, "")]
    while pending:
        remote_directory, relative_directory = pending.pop()
        for entry in list_remote(api, remote_directory):
            name = str(entry["name"])
            if name in (".", "..") or "/" in name:
                raise Nass3cpError("NAS returned an unsafe directory entry name")
            relative = _relative_child(relative_directory, name)
            if relative in entry_types:
                raise Nass3cpError("NAS returned a duplicate recursive path: %s" % relative)
            kind = str(entry["type"])
            entry_types[relative] = kind
            if kind == "directory":
                directories.append(relative)
                pending.append((_remote_child(root, relative), relative))
            elif kind == "file":
                files.append(
                    TreeFile(
                        relative=relative,
                        size=int(entry["size"]),
                        mtime_ns=int(entry["mtime_ns"]),
                    )
                )
            else:
                unsupported.append(relative)
    directories.sort(key=lambda value: (value.count("/"), value.casefold(), value))
    files.sort(key=lambda item: (item.relative.casefold(), item.relative))
    unsupported.sort(key=lambda value: (value.casefold(), value))
    return TreeSnapshot(True, "directory", directories, files, entry_types, unsupported)


def _validate_policy(policy: str) -> None:
    if policy not in ("auto", "raw"):
        raise ValueError("recursive policy must be auto or raw")


def _sort_transfers(files: Sequence[TreeFile], policy: str) -> List[TreeFile]:
    return sorted(
        files,
        key=lambda item: (
            0 if policy == "auto" and should_compress(item.relative) else 1,
            item.relative.casefold(),
            item.relative,
        ),
    )


def _print_plan(plan: RecursivePlan, direction: str, dry: bool, stream: TextIO) -> None:
    candidates = plan.compression_candidates
    candidate_size = sum(item.size for item in candidates)
    print("recursive copy plan:", file=stream)
    print("  direction: %s" % direction, file=stream)
    print("  policy: %s" % plan.policy, file=stream)
    print("  source directories: %d" % (len(plan.source.directories) + 1), file=stream)
    print("  source files: %d" % len(plan.source.files), file=stream)
    print(
        "  original size: %d bytes (%s)" % (plan.original_size, _human_bytes(plan.original_size)),
        file=stream,
    )
    print(
        "  existing files skipped: %d, %d bytes (%s)"
        % (len(plan.skipped), plan.skipped_size, _human_bytes(plan.skipped_size)),
        file=stream,
    )
    print(
        "  files to transfer: %d, %d original bytes (%s)"
        % (len(plan.transfers), plan.transfer_size, _human_bytes(plan.transfer_size)),
        file=stream,
    )
    print(
        "  auto compression candidates: %d, %d original bytes (%s)"
        % (len(candidates), candidate_size, _human_bytes(candidate_size)),
        file=stream,
    )
    print("  directories to create: %d" % len(plan.directories_to_create), file=stream)
    print("  unsupported source entries skipped: %d" % len(plan.source.unsupported), file=stream)
    print("  path conflicts: %d" % len(plan.conflicts), file=stream)
    for conflict in plan.conflicts[:10]:
        print("    conflict: %s" % conflict, file=stream)
    if len(plan.conflicts) > 10:
        print("    ... and %d more" % (len(plan.conflicts) - 10), file=stream)
    if dry:
        print("  dry run: no directories or files were changed", file=stream)


def _local_upload_plan(
    source: TreeSnapshot,
    destination: TreeSnapshot,
    policy: str,
) -> RecursivePlan:
    directories_to_create: List[str] = []
    conflicts: List[str] = []
    if not destination.root_exists:
        directories_to_create.append("")
    elif destination.root_type != "directory":
        conflicts.append("destination root exists as a %s" % destination.root_type)
    for relative in source.directories:
        existing = destination.entry_types.get(relative)
        if existing is None:
            directories_to_create.append(relative)
        elif existing != "directory":
            conflicts.append("%s is a %s at the NAS destination" % (relative, existing))

    transfers: List[TreeFile] = []
    skipped: List[TreeFile] = []
    for item in source.files:
        existing = destination.entry_types.get(item.relative)
        if existing is None:
            transfers.append(item)
        elif existing == "file":
            skipped.append(item)
        elif existing == "directory":
            conflicts.append("%s is a directory at the NAS destination" % item.relative)
        else:
            conflicts.append("%s is a %s at the NAS destination" % (item.relative, existing))
    return RecursivePlan(
        source=source,
        transfers=_sort_transfers(transfers, policy),
        skipped=skipped,
        directories_to_create=directories_to_create,
        conflicts=conflicts,
        policy=policy,
    )


def _local_download_plan(source: TreeSnapshot, destination_root: Path, policy: str) -> RecursivePlan:
    directories_to_create: List[str] = []
    conflicts: List[str] = []
    root_type = _local_type(destination_root)
    if root_type is None:
        directories_to_create.append("")
    elif root_type != "directory":
        conflicts.append("destination root exists as a %s" % root_type)

    for relative in source.directories:
        try:
            existing = _local_type(_local_child(destination_root, relative))
        except Nass3cpError as exc:
            conflicts.append(str(exc))
            continue
        if existing is None:
            directories_to_create.append(relative)
        elif existing != "directory":
            conflicts.append("%s is a %s at the local destination" % (relative, existing))

    transfers: List[TreeFile] = []
    skipped: List[TreeFile] = []
    for item in source.files:
        try:
            existing = _local_type(_local_child(destination_root, item.relative))
        except Nass3cpError as exc:
            conflicts.append(str(exc))
            continue
        if existing is None:
            transfers.append(item)
        elif existing == "file":
            skipped.append(item)
        elif existing == "directory":
            conflicts.append("%s is a directory at the local destination" % item.relative)
        else:
            conflicts.append("%s is a %s at the local destination" % (item.relative, existing))
    return RecursivePlan(
        source=source,
        transfers=_sort_transfers(transfers, policy),
        skipped=skipped,
        directories_to_create=directories_to_create,
        conflicts=conflicts,
        policy=policy,
    )


def _compressed_local_payload(
    item: TreeFile,
    progress: Optional[Callable[[int], None]] = None,
) -> Tuple[Path, Optional[str], Optional[str], bool]:
    if item.local_path is None:
        raise Nass3cpError("local source path is missing from recursive plan")
    fd, temporary_name = tempfile.mkstemp(prefix="nass3cp-", suffix=".gz")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output, item.local_path.open("rb") as source:
            before = os.fstat(source.fileno())
            if (
                before.st_dev != item.device
                or before.st_ino != item.inode
                or before.st_size != item.size
                or before.st_mtime_ns != item.mtime_ns
            ):
                raise Nass3cpError("local source changed after recursive planning: %s" % item.relative)
            digest, decoded_size = gzip_compress_stream(source, output, progress)
            output.flush()
            os.fsync(output.fileno())
            after = os.fstat(source.fileno())
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or decoded_size != item.size
            ):
                raise Nass3cpError("local source changed during compression: %s" % item.relative)
        if temporary.stat().st_size >= item.size:
            temporary.unlink()
            return item.local_path, None, None, False
        return temporary, "gzip", digest, True
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def recursive_upload_directory(
    api: ApiClient,
    local_source: str,
    remote_destination: str,
    policy: str,
    dry: bool,
    jobs: int,
    inflight: int,
    transfer_timeout: int,
    quiet: bool,
    output: Optional[TextIO] = None,
) -> RecursivePlan:
    _validate_policy(policy)
    source = scan_local_tree(local_source)
    destination = scan_remote_tree(
        api,
        remote_destination,
        missing_ok=True,
        non_directory_ok=True,
    )
    plan = _local_upload_plan(source, destination, policy)
    stream = output if output is not None else sys.stdout
    if dry:
        _print_plan(plan, "local -> NAS", True, stream)
        return plan
    if plan.conflicts:
        _print_plan(plan, "local -> NAS", False, stream)
        raise Nass3cpError("recursive destination has %d path conflict(s)" % len(plan.conflicts))

    for relative in plan.directories_to_create:
        api.ensure_directory(_remote_child(remote_destination, relative))

    copied = 0
    newly_skipped = 0
    for number, item in enumerate(plan.transfers, 1):
        if item.local_path is None:
            raise Nass3cpError("local source path is missing from recursive plan")
        remote_path = _remote_child(remote_destination, item.relative)
        appeared = api.path_info(remote_path)
        if appeared["exists"]:
            if appeared.get("type") == "file":
                newly_skipped += 1
                if not quiet:
                    print(
                        "recursive upload skipped newly existing %s" % item.relative,
                        file=sys.stderr,
                    )
                continue
            raise Nass3cpError(
                "NAS destination appeared as a %s during recursive copy: %s"
                % (appeared.get("type"), remote_path)
            )
        payload = item.local_path
        compression: Optional[str] = None
        decoded_digest: Optional[str] = None
        temporary = False
        try:
            if policy == "auto" and should_compress(item.relative):
                compression_progress = _ProgressDisplay(not quiet)
                compression_label = "compress locally %d/%d" % (number, len(plan.transfers))
                try:
                    compression_progress.update(
                        compression_label,
                        0,
                        item.size,
                        force=True,
                    )
                    payload, compression, decoded_digest, temporary = _compressed_local_payload(
                        item,
                        lambda processed: compression_progress.update(
                            compression_label,
                            processed,
                            item.size,
                            force=processed == item.size,
                        ),
                    )
                finally:
                    compression_progress.close()
            if not quiet:
                print(
                    "recursive upload %d/%d [%s] %s"
                    % (number, len(plan.transfers), compression or "raw", item.relative),
                    file=sys.stderr,
                )
            upload(
                api,
                str(payload),
                remote_path,
                False,
                jobs,
                transfer_timeout,
                quiet,
                inflight,
                compression=compression,
                decoded_size=item.size if compression else None,
                decoded_digest=decoded_digest,
                destination_mtime_ns=item.mtime_ns,
                source_identity=(item.device, item.inode, item.size, item.mtime_ns)
                if compression is None
                and item.device is not None
                and item.inode is not None
                else None,
            )
        finally:
            if temporary:
                try:
                    payload.unlink()
                except OSError:
                    pass
        copied += 1
    if not quiet:
        print(
            "recursive upload complete: %d copied, %d existing skipped, %d unsupported skipped"
            % (copied, len(plan.skipped) + newly_skipped, len(plan.source.unsupported)),
            file=sys.stderr,
        )
    return plan


def recursive_download_directory(
    api: ApiClient,
    remote_source: str,
    local_destination: str,
    policy: str,
    dry: bool,
    jobs: int,
    inflight: int,
    transfer_timeout: int,
    quiet: bool,
    output: Optional[TextIO] = None,
) -> RecursivePlan:
    _validate_policy(policy)
    source = scan_remote_tree(api, remote_source)
    destination_root = Path(local_destination)
    plan = _local_download_plan(source, destination_root, policy)
    stream = output if output is not None else sys.stdout
    if dry:
        _print_plan(plan, "NAS -> local", True, stream)
        return plan
    if plan.conflicts:
        _print_plan(plan, "NAS -> local", False, stream)
        raise Nass3cpError("recursive destination has %d path conflict(s)" % len(plan.conflicts))

    for relative in plan.directories_to_create:
        destination = destination_root if not relative else _local_child(destination_root, relative)
        destination.mkdir(parents=True, exist_ok=True)

    copied = 0
    newly_skipped = 0
    for number, item in enumerate(plan.transfers, 1):
        destination = _local_child(destination_root, item.relative)
        appeared = _local_type(destination)
        if appeared == "file":
            newly_skipped += 1
            if not quiet:
                print("recursive download skipped newly existing %s" % item.relative, file=sys.stderr)
            continue
        if appeared is not None:
            raise Nass3cpError(
                "local destination appeared as a %s during recursive copy: %s"
                % (appeared, destination)
            )
        compression = "gzip" if policy == "auto" and should_compress(item.relative) else None
        if not quiet:
            print(
                "recursive download %d/%d [%s] %s"
                % (number, len(plan.transfers), compression or "raw", item.relative),
                file=sys.stderr,
            )
        download(
            api,
            _remote_child(remote_source, item.relative),
            str(destination),
            False,
            jobs,
            transfer_timeout,
            quiet,
            inflight,
            compression=compression,
        )
        copied += 1
    if not quiet:
        print(
            "recursive download complete: %d copied, %d existing skipped, %d unsupported skipped"
            % (copied, len(plan.skipped) + newly_skipped, len(plan.source.unsupported)),
            file=sys.stderr,
        )
    return plan
