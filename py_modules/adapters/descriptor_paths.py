"""Descriptor-relative exact-identity mutation helpers for recovery-backed cleanup."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import signal
import stat
import struct
import threading
from typing import TYPE_CHECKING

from lib.errors import OperationAbortedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from models.prune import MutationOutcome, SourceClaim, SourceEntry, SourceIdentity

_F_SETOWN_EX = 15
_F_OWNER_TID = 0
_RENAME_NOREPLACE = 1
_ALREADY_ABSENT = "Source was already absent"
_EXCLUSION_DRIFT = "Recovery source subtree changed before writer exclusion"
_CONSUMED_DRIFT = "Recovery source subtree changed while it was consumed"


def identity_for_stat(value: os.stat_result, mount_id: int = 0) -> SourceIdentity:
    return {
        "exists": True,
        "mount_id": mount_id,
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": value.st_mode,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def missing_identity() -> SourceIdentity:
    return {
        "exists": False,
        "mount_id": 0,
        "device": 0,
        "inode": 0,
        "mode": 0,
        "size": 0,
        "mtime_ns": 0,
        "ctime_ns": 0,
    }


def raise_if_aborted(should_abort: Callable[[], bool] | None) -> None:
    """Stop the current worker when its caller has asked it to.

    The single place a cooperative stop becomes an exception, so every long
    loop below reports an abort the same way and callers have one type to
    recognise.
    """
    if should_abort is not None and should_abort():
        raise OperationAbortedError("The operation was cancelled before it committed anything.")


def claim_source(
    path: str,
    safe_root: str,
    should_abort: Callable[[], bool] | None = None,
    *,
    digest: bool = True,
) -> SourceClaim:
    """Capture a complete no-follow identity claim for one source tree.

    *should_abort* makes claiming a large tree interruptible: it is polled per
    directory entry and per hashed chunk, and a true answer raises
    :class:`OperationAbortedError` rather than finishing the walk.

    *digest* decides whether every regular file is also content-hashed. A hash
    binds the claim to bytes held somewhere else — a recovery bundle's copy —
    and costs one full read of the tree per validation pass. A claim sealed and
    consumed by the same caller has no such second copy to bind to, so it takes
    the identity-only form and the claim records which discipline authorized it.
    """
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        return _claim(path, safe_root, missing_identity(), None, {}, digest)
    try:
        current = _stat_name(parent_fd, name)
        if current is None:
            return _claim(path, safe_root, missing_identity(), None, {}, digest)
        if stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(parent_fd, name)
            try:
                _require_same_mount(parent_fd, file_fd, path)
                identity = _identity_for_fd(file_fd)
                _require_entry_matches_fd(path, current, identity)
                sha256 = None
                if digest:
                    sha256 = _sha256_fd(file_fd, should_abort)
                    if _identity_for_fd(file_fd) != identity:
                        raise RuntimeError(f"Recovery source changed while it was claimed: {path}")
            finally:
                os.close(file_fd)
            return _claim(path, safe_root, identity, sha256, {}, digest)
        if stat.S_ISLNK(current.st_mode):
            raise ValueError(f"Recovery source may not be a symlink: {path}")
        if not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"Recovery source has unsupported type: {path}")
        directory_fd = _open_child_directory(parent_fd, name)
        try:
            _require_same_mount(parent_fd, directory_fd, path)
            identity = _identity_for_fd(directory_fd)
            _require_entry_matches_fd(path, current, identity)
            entries = _inventory_directory(directory_fd, identity["mount_id"], should_abort=should_abort, digest=digest)
        finally:
            os.close(directory_fd)
        return _claim(path, safe_root, identity, None, entries, digest)
    finally:
        os.close(parent_fd)


def staging_prefix(name: str) -> str:
    """Return the prefix a staged-away *name* is renamed to before it is unlinked.

    An interrupted removal leaves such an entry under the source's own parent,
    so the reclaim path needs the same format the mutation writes.
    """
    return f".{name}.romm-prune-"


def measure_tree(path: str, safe_root: str) -> int:
    """Sum every regular-file byte below one source tree without reading its content.

    Applies the same anchored, no-follow, single-mount discipline as
    :func:`claim_source`, but takes no identity claim and computes no hash — a
    missing source measures as ``0``, an unsupported entry refuses.
    """
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        return 0
    try:
        current = _stat_name(parent_fd, name)
        if current is None:
            return 0
        if stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(parent_fd, name)
            try:
                _require_same_mount(parent_fd, file_fd, path)
                return os.fstat(file_fd).st_size
            finally:
                os.close(file_fd)
        if stat.S_ISLNK(current.st_mode):
            raise ValueError(f"Recovery source may not be a symlink: {path}")
        if not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"Recovery source has unsupported type: {path}")
        directory_fd = _open_child_directory(parent_fd, name)
        try:
            _require_same_mount(parent_fd, directory_fd, path)
            return _measure_directory(directory_fd, _mount_id(directory_fd))
        finally:
            os.close(directory_fd)
    finally:
        os.close(parent_fd)


def remove_claimed(
    path: str,
    safe_root: str,
    claim: SourceClaim,
    on_progress: Callable[[int, int], None] | None = None,
) -> MutationOutcome:
    """Claim, revalidate, and durably remove exactly one source tree.

    *on_progress* is called after each regular file's unlink with the number
    removed so far and the claim's total, on the calling thread.
    """
    _require_claim_shape(path, safe_root, claim)
    expected = claim["source_identity"]
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        if expected["exists"]:
            raise RuntimeError(f"Recovery source disappeared after sealing: {path}") from None
        return _outcome(success=True, changed=False, ambiguous=False, message=_ALREADY_ABSENT)
    try:
        current = _stat_name(parent_fd, name)
        _require_identity(path, current, expected)
        if current is None:
            return _outcome(success=True, changed=False, ambiguous=False, message=_ALREADY_ABSENT)
        temporary = f"{staging_prefix(name)}{current.st_ino}"
        if _stat_name(parent_fd, temporary) is not None:
            raise FileExistsError(f"Prune staging entry already exists: {temporary}")
        rename_noreplace_at(parent_fd, name, parent_fd, temporary)
        claimed = _stat_name(parent_fd, temporary)
        lease_stack = contextlib.ExitStack()
        leased_files: dict[str, int] = {}
        progress = _RemovalProgress(_claimed_file_count(claim), on_progress)
        try:
            _require_claimed_identity(path, claimed, expected)
            _lease_claimed_source(path, parent_fd, temporary, current, claim, lease_stack, leased_files)
        except Exception:
            rollback = _roll_back_claim(parent_fd, name, temporary, lease_stack)
            if rollback is not None:
                return rollback
            raise
        removal_error: BaseException | None = None
        try:
            _unlink_claimed_source(path, parent_fd, temporary, current, claim, leased_files, progress)
        except Exception as exc:
            removal_error = exc
        try:
            lease_stack.close()
        except Exception as exc:
            return _outcome(
                success=False,
                changed=True,
                ambiguous=True,
                message=f"Source was removed but writer-exclusion teardown is uncertain: {exc}",
            )
        if removal_error is not None:
            return _outcome(
                success=False,
                changed=True,
                ambiguous=True,
                message=f"Source removal stopped after {progress.summary()}: {removal_error}",
            )
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            return _outcome(
                success=False,
                changed=True,
                ambiguous=True,
                message=f"Source was removed but directory durability is uncertain: {exc}",
            )
        return _outcome(success=True, changed=True, ambiguous=False, message="Source removed")
    finally:
        os.close(parent_fd)


def remove_current(path: str, safe_root: str) -> MutationOutcome:
    """Remove the current entry through anchored parents without following symlinks."""
    return remove_claimed(path, safe_root, claim_source(path, safe_root))


def rename_claimed(src: str, dst: str, safe_root: str, claim: SourceClaim) -> MutationOutcome:
    """Durably rename only the exact claimed source through anchored parents."""
    _require_claim_shape(src, safe_root, claim)
    expected = claim["source_identity"]
    source_fd, source_name = _open_parent(src, safe_root)
    destination_fd, destination_name = _open_parent(dst, safe_root)
    try:
        current = _stat_name(source_fd, source_name)
        _require_identity(src, current, expected)
        if current is None:
            return _outcome(success=True, changed=False, ambiguous=False, message=_ALREADY_ABSENT)
        _require_claim_before_rename(src, source_fd, source_name, current, claim)
        if _stat_name(destination_fd, destination_name) is not None:
            raise FileExistsError(f"Recovery destination already exists: {dst}")
        try:
            rename_noreplace_at(source_fd, source_name, destination_fd, destination_name)
        except FileExistsError:
            raise FileExistsError(f"Recovery destination already exists: {dst}") from None
        claimed = _stat_name(destination_fd, destination_name)
        try:
            _require_claimed_identity(src, claimed, expected)
            _require_claim_after_rename(src, destination_fd, destination_name, current, claim)
        except Exception as exc:
            rollback = _roll_back_rename(dst, source_fd, source_name, destination_fd, destination_name, exc)
            if rollback is not None:
                return rollback
            raise
        try:
            os.fsync(source_fd)
            if destination_fd != source_fd:
                os.fsync(destination_fd)
        except OSError as exc:
            return _outcome(
                success=False,
                changed=True,
                ambiguous=True,
                message=f"Source was renamed but directory durability is uncertain: {exc}",
            )
        return _outcome(success=True, changed=True, ambiguous=False, message="Source renamed")
    finally:
        os.close(source_fd)
        os.close(destination_fd)


def ensure_directory(path: str, safe_root: str, mode: int = 0o700) -> None:
    """Create and open each directory component beneath an anchored root."""
    parts = _relative_parts(path, safe_root)
    fd = _open_directory(safe_root, safe_root)
    try:
        root_mount_id = _mount_id(fd)
        for component in parts:
            try:
                next_fd = _open_child_directory(fd, component)
            except FileNotFoundError:
                os.mkdir(component, mode, dir_fd=fd)
                os.fsync(fd)
                next_fd = _open_child_directory(fd, component)
            if _mount_id(next_fd) != root_mount_id:
                os.close(next_fd)
                raise ValueError(f"Path crosses a mount boundary: {path}")
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def require_directory(path: str, safe_root: str) -> None:
    """Require every component through *path* to be a real directory, never a symlink."""
    fd = _open_directory(path, safe_root)
    os.close(fd)


def open_directory_fd(path: str, safe_root: str) -> int:
    """Open an anchored directory path with no-follow traversal."""
    return _open_directory(path, safe_root)


def open_regular_fd(path: str, safe_root: str) -> int:
    """Open an anchored regular file with no-follow traversal."""
    parent_fd, name = _open_parent(path, safe_root)
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f"Expected a regular file: {path}")
        return fd
    finally:
        os.close(parent_fd)


def mount_id_for_fd(fd: int) -> int:
    """Return Linux's descriptor-bound mount identity, failing closed if unavailable."""
    return _mount_id(fd)


def _open_parent(path: str, safe_root: str) -> tuple[int, str]:
    parts = _relative_parts(path, safe_root)
    if not parts:
        raise ValueError(f"Path must be below its safe root: {path}")
    parent = os.path.join(safe_root, *parts[:-1]) if len(parts) > 1 else safe_root
    return _open_directory(parent, safe_root), parts[-1]


def _open_directory(path: str, safe_root: str) -> int:
    root = os.path.realpath(os.path.abspath(safe_root))
    parts = _relative_parts(path, safe_root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    root_mount_id = _mount_id(fd)
    try:
        for component in parts:
            next_fd = os.open(component, flags, dir_fd=fd)
            if _mount_id(next_fd) != root_mount_id:
                os.close(next_fd)
                raise ValueError(f"Path crosses a mount boundary: {path}")
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _relative_parts(path: str, safe_root: str) -> list[str]:
    absolute_root = os.path.abspath(safe_root)
    absolute_path = os.path.abspath(path)
    try:
        if os.path.commonpath((absolute_root, absolute_path)) != absolute_root:
            raise ValueError(f"Path is outside its safe root: {path}")
    except ValueError as exc:
        raise ValueError(f"Path is outside its safe root: {path}") from exc
    relative = os.path.relpath(absolute_path, absolute_root)
    if relative == ".":
        return []
    parts = relative.split(os.sep)
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe path component: {path}")
    return parts


def _stat_name(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=parent_fd)


def _open_child_regular(parent_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)


def rename_noreplace_at(source_fd: int, source_name: str, destination_fd: int, destination_name: str) -> None:
    """Atomically rename one descriptor-relative entry without replacing a destination."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2(RENAME_NOREPLACE) is unavailable") from exc
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination_name)
    raise OSError(error, os.strerror(error), source_name)


@contextlib.contextmanager
def hold_writer_exclusion(fd: int, path: str):
    """Exclude external writers from an already-open regular file descriptor."""
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGIO})
    leased = False
    try:
        fcntl.fcntl(
            fd,
            _F_SETOWN_EX,
            struct.pack("=ii", _F_OWNER_TID, threading.get_native_id()),
        )
        fcntl.fcntl(fd, fcntl.F_SETSIG, signal.SIGIO)
        try:
            fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        except OSError as exc:
            raise RuntimeError(f"Recovery source has an active writer and was retained: {path}") from exc
        leased = True
        yield fd
    finally:
        release_error: OSError | None = None
        if leased:
            try:
                fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
            except OSError as exc:
                release_error = exc
        if signal.SIGIO in signal.sigpending():
            signal.sigwait({signal.SIGIO})
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if release_error is not None:
            raise release_error


@contextlib.contextmanager
def _leased_regular(parent_fd: int, name: str, path: str):
    """Open a regular file and exclude every external writer until context exit."""
    fd = _open_child_regular(parent_fd, name)
    try:
        with hold_writer_exclusion(fd, path):
            yield fd
    finally:
        os.close(fd)


def _inventory_directory(
    directory_fd: int,
    mount_id: int,
    prefix: str = "",
    *,
    should_abort: Callable[[], bool] | None = None,
    digest: bool = True,
) -> dict[str, SourceEntry]:
    entries: dict[str, SourceEntry] = {}
    for name in sorted(os.listdir(directory_fd)):
        raise_if_aborted(should_abort)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                identity = _identity_for_fd(child_fd)
                _require_entry_matches_fd(relative, current, identity)
                entries[relative] = {"identity": identity}
                entries.update(
                    _inventory_directory(child_fd, mount_id, relative, should_abort=should_abort, digest=digest)
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(directory_fd, name)
            try:
                _require_mount_id(file_fd, mount_id, relative)
                identity = _identity_for_fd(file_fd)
                _require_entry_matches_fd(relative, current, identity)
                entry: SourceEntry = {"identity": identity}
                if digest:
                    entry["sha256"] = _sha256_fd(file_fd, should_abort)
                    if _identity_for_fd(file_fd) != identity:
                        raise RuntimeError(f"Recovery source changed while inventorying: {relative}")
                entries[relative] = entry
            finally:
                os.close(file_fd)
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")
    return entries


def _measure_directory(directory_fd: int, mount_id: int, prefix: str = "") -> int:
    total = 0
    for name in sorted(os.listdir(directory_fd)):
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                total += _measure_directory(child_fd, mount_id, relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(directory_fd, name)
            try:
                _require_mount_id(file_fd, mount_id, relative)
                total += os.fstat(file_fd).st_size
            finally:
                os.close(file_fd)
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")
    return total


def _sha256_fd(fd: int, should_abort: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, 1024 * 1024):
        raise_if_aborted(should_abort)
        digest.update(block)
    return digest.hexdigest()


def _claim(
    path: str,
    safe_root: str,
    identity: SourceIdentity,
    sha256: str | None,
    entries: dict[str, SourceEntry],
    content_bound: bool,
) -> SourceClaim:
    return {
        "source_path": path,
        "safe_root": safe_root,
        "source_identity": identity,
        "sha256": sha256,
        "entries": entries,
        "content_bound": content_bound,
    }


class _RemovalProgress:
    """Counts unlinked regular files and reports each one to an optional observer."""

    def __init__(self, total: int, on_progress: Callable[[int, int], None] | None) -> None:
        self._total = total
        self._on_progress = on_progress
        self._removed = 0

    def tick(self) -> None:
        self._removed += 1
        if self._on_progress is not None:
            self._on_progress(self._removed, self._total)

    def summary(self) -> str:
        """How far the removal got, for a stopped removal's report."""
        if self._removed == 0:
            return "the source was claimed and no file was removed"
        return f"{self._removed} of {self._total} files were removed"


def _claimed_file_count(claim: SourceClaim) -> int:
    """Count the regular files one claim authorizes removing."""
    if not claim["source_identity"]["exists"]:
        return 0
    if stat.S_ISDIR(claim["source_identity"]["mode"]):
        return sum(1 for entry in claim["entries"].values() if stat.S_ISREG(entry["identity"]["mode"]))
    return 1


def _require_claim_shape(path: str, safe_root: str, claim: SourceClaim) -> None:
    if claim["source_path"] != path or claim["safe_root"] != safe_root:
        raise ValueError(f"Source claim does not match its mutation target: {path}")


def _restore_claim(parent_fd: int, name: str, temporary: str) -> None:
    if _stat_name(parent_fd, name) is not None:
        raise RuntimeError(f"Cannot restore claimed source because its path was replaced: {name}")
    rename_noreplace_at(parent_fd, temporary, parent_fd, name)
    os.fsync(parent_fd)


def _lease_claimed_source(
    path: str,
    parent_fd: int,
    temporary: str,
    current: os.stat_result,
    claim: SourceClaim,
    lease_stack: contextlib.ExitStack,
    leased_files: dict[str, int],
) -> None:
    """Hold writer exclusion over the claimed source and refuse any drift since sealing.

    A content-bound claim holds every descendant's lease from here through the
    whole tree's unlink, so the removal is all-or-nothing. An identity-only
    directory cannot: a tree of tens of thousands of files would exhaust the
    process's descriptor limit and become impossible to remove at all. It proves
    the same thing one file at a time instead — *leased_files* stays empty and
    each file is leased again for its own unlink.
    """
    expected = claim["source_identity"]
    content_bound = claim["content_bound"]
    if stat.S_ISDIR(current.st_mode):
        directory_fd = _open_child_directory(parent_fd, temporary)
        try:
            _require_same_mount(parent_fd, directory_fd, path)
            actual_entries = _inventory_directory(directory_fd, expected["mount_id"], digest=content_bound)
        finally:
            os.close(directory_fd)
        if actual_entries != claim["entries"]:
            raise RuntimeError(f"Recovery source subtree changed after sealing: {path}")
        directory_fd = _open_child_directory(parent_fd, temporary)
        try:
            if content_bound:
                _acquire_directory_leases(
                    directory_fd,
                    claim["entries"],
                    expected["mount_id"],
                    lease_stack,
                    leased_files,
                )
            _validate_claimed_directory(
                directory_fd,
                claim["entries"],
                expected["mount_id"],
                leased_files=leased_files,
                content_bound=content_bound,
            )
        finally:
            os.close(directory_fd)
    elif claim["entries"]:
        raise ValueError(f"Regular-file recovery claim has descendants: {path}")
    else:
        file_fd = lease_stack.enter_context(_leased_regular(parent_fd, temporary, path))
        leased_files[""] = file_fd
        _require_same_mount(parent_fd, file_fd, path)
        _require_file_claim(path, file_fd, expected, claim["sha256"], claimed=True, content_bound=content_bound)


def _unlink_claimed_source(
    path: str,
    parent_fd: int,
    temporary: str,
    current: os.stat_result,
    claim: SourceClaim,
    leased_files: dict[str, int],
    progress: _RemovalProgress,
) -> None:
    """Delete the claimed source, each regular file re-proved immediately before its unlink."""
    expected = claim["source_identity"]
    if stat.S_ISDIR(current.st_mode):
        directory_fd = _open_child_directory(parent_fd, temporary)
        try:
            _delete_claimed_directory(
                directory_fd,
                claim["entries"],
                expected["mount_id"],
                leased_files=leased_files,
                content_bound=claim["content_bound"],
                progress=progress,
            )
        finally:
            os.close(directory_fd)
        os.rmdir(temporary, dir_fd=parent_fd)
    else:
        file_fd = leased_files[""]
        _require_file_claim(
            path, file_fd, expected, claim["sha256"], claimed=True, content_bound=claim["content_bound"]
        )
        current_claim = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        _require_entry_matches_fd(path, current_claim, _identity_for_fd(file_fd))
        os.unlink(temporary, dir_fd=parent_fd)
        progress.tick()


def _require_claim_before_rename(
    src: str,
    source_fd: int,
    source_name: str,
    current: os.stat_result,
    claim: SourceClaim,
) -> None:
    """Refuse a rename whose source drifted from the sealed claim."""
    expected = claim["source_identity"]
    content_bound = claim["content_bound"]
    if stat.S_ISDIR(current.st_mode):
        directory_fd = _open_child_directory(source_fd, source_name)
        try:
            _require_same_mount(source_fd, directory_fd, src)
            if _inventory_directory(directory_fd, expected["mount_id"], digest=content_bound) != claim["entries"]:
                raise RuntimeError(f"Recovery source subtree changed after sealing: {src}")
        finally:
            os.close(directory_fd)
    elif claim["entries"]:
        raise ValueError(f"Regular-file recovery claim has descendants: {src}")
    else:
        file_fd = _open_child_regular(source_fd, source_name)
        try:
            _require_same_mount(source_fd, file_fd, src)
            _require_file_claim(src, file_fd, expected, claim["sha256"], claimed=False, content_bound=content_bound)
        finally:
            os.close(file_fd)


def _require_claim_after_rename(
    src: str,
    destination_fd: int,
    destination_name: str,
    current: os.stat_result,
    claim: SourceClaim,
) -> None:
    """Refuse a rename whose source drifted while it was moved."""
    expected = claim["source_identity"]
    content_bound = claim["content_bound"]
    if stat.S_ISDIR(current.st_mode):
        directory_fd = _open_child_directory(destination_fd, destination_name)
        try:
            _require_same_mount(destination_fd, directory_fd, src)
            if _inventory_directory(directory_fd, expected["mount_id"], digest=content_bound) != claim["entries"]:
                raise RuntimeError(f"Recovery source subtree changed while it was renamed: {src}")
        finally:
            os.close(directory_fd)
    else:
        file_fd = _open_child_regular(destination_fd, destination_name)
        try:
            _require_same_mount(destination_fd, file_fd, src)
            _require_file_claim(src, file_fd, expected, claim["sha256"], claimed=True, content_bound=content_bound)
        finally:
            os.close(file_fd)


def _roll_back_rename(
    dst: str,
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    exc: Exception,
) -> MutationOutcome | None:
    """Move a refused rename back. ``None`` means the refusal itself must surface."""
    reoccupied = False
    try:
        reoccupied = _stat_name(source_fd, source_name) is not None
        if not reoccupied:
            rename_noreplace_at(destination_fd, destination_name, source_fd, source_name)
            os.fsync(source_fd)
            if destination_fd != source_fd:
                os.fsync(destination_fd)
    except Exception as restore_exc:
        return _outcome(
            success=False,
            changed=True,
            ambiguous=True,
            message=f"Rename validation failed and rollback was uncertain: {restore_exc}",
        )
    if reoccupied:
        return _outcome(
            success=False,
            changed=True,
            ambiguous=True,
            message=(
                f"Rename validation failed and the source is still at {dst} "
                f"because its original path was re-occupied: {exc}"
            ),
        )
    return None


def _roll_back_claim(
    parent_fd: int,
    name: str,
    temporary: str,
    lease_stack: contextlib.ExitStack,
) -> MutationOutcome | None:
    """Give a refused claim back. ``None`` means the refusal itself must surface."""
    close_error: BaseException | None = None
    try:
        lease_stack.close()
    except Exception as exc:
        close_error = exc
    try:
        _restore_claim(parent_fd, name, temporary)
    except Exception as restore_exc:
        return _outcome(
            success=False,
            changed=True,
            ambiguous=True,
            message=f"Source validation failed and rollback was uncertain: {restore_exc}",
        )
    if close_error is not None:
        raise RuntimeError(f"Writer-exclusion teardown failed after source validation: {close_error}") from close_error
    return None


def _outcome(*, success: bool, changed: bool, ambiguous: bool, message: str) -> MutationOutcome:
    return {"success": success, "changed": changed, "ambiguous": ambiguous, "message": message}


def _require_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    if not expected["exists"]:
        if current is not None:
            raise RuntimeError(f"Recovery source appeared after sealing: {path}")
        return
    if current is None:
        raise RuntimeError(f"Recovery source disappeared after sealing: {path}")
    actual = identity_for_stat(current, expected["mount_id"])
    fields = ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
    if any(actual[field] != expected[field] for field in fields):
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")


def _require_claimed_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    """Compare a claimed entry's stat against its sealed identity.

    A bare ``stat_result`` carries no mount identity, so mount revalidation is
    not performed here — every caller re-checks it against a real descriptor via
    :func:`_require_same_mount` or :func:`_require_file_claim` immediately after.
    """
    if current is None or not expected["exists"]:
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
    actual = identity_for_stat(current, expected["mount_id"])
    stable_fields = ("device", "inode", "mode", "size", "mtime_ns")
    if any(actual[field] != expected[field] for field in stable_fields):
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")


def _identity_for_fd(fd: int) -> SourceIdentity:
    return identity_for_stat(os.fstat(fd), _mount_id(fd))


def _mount_id(fd: int) -> int:
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
        for line in info:
            key, separator, value = line.partition(":")
            if key == "mnt_id" and separator:
                return int(value.strip())
    raise OSError(f"Mount identity is unavailable for descriptor {fd}")


def _require_same_mount(parent_fd: int, child_fd: int, path: str) -> None:
    _require_mount_id(child_fd, _mount_id(parent_fd), path)


def _require_mount_id(fd: int, expected: int, path: str) -> None:
    if _mount_id(fd) != expected:
        raise ValueError(f"Recovery source crosses a mount boundary: {path}")


def _require_entry_matches_fd(path: str, entry: os.stat_result, identity: SourceIdentity) -> None:
    fields = ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
    entry_identity = identity_for_stat(entry, identity["mount_id"])
    if any(entry_identity[field] != identity[field] for field in fields):
        raise RuntimeError(f"Recovery source changed while opening: {path}")


def _require_file_claim(
    path: str,
    fd: int,
    expected: SourceIdentity,
    expected_hash: str | None,
    *,
    claimed: bool,
    content_bound: bool = True,
) -> None:
    if content_bound and expected_hash is None:
        raise ValueError(f"Regular-file recovery claim has no content hash: {path}")
    before = _identity_for_fd(fd)
    stable_fields = ("mount_id", "device", "inode", "mode", "size", "mtime_ns") if claimed else tuple(expected)
    if any(before[field] != expected[field] for field in stable_fields if field != "exists"):
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")
    if expected_hash is None:
        return
    if _sha256_fd(fd) != expected_hash or _identity_for_fd(fd) != before:
        raise RuntimeError(f"Recovery source content changed after sealing: {path}")


def _claimed_children(
    directory_fd: int,
    entries: dict[str, SourceEntry],
    prefix: str,
    drift: str,
) -> Iterator[tuple[str, str, SourceEntry, os.stat_result]]:
    """Walk one claimed directory level in name order, refusing it whole on any drift.

    The listing must match the claimed names exactly before a single child is
    handed out, so a create, unlink, or rename anywhere at this level refuses the
    subtree instead of letting the caller act on the entries that still match.
    """
    expected_names = {
        relative[len(prefix) + 1 :].split("/", 1)[0] if prefix else relative.split("/", 1)[0]
        for relative in entries
        if not prefix or relative.startswith(prefix + "/")
    }
    actual_names = set(os.listdir(directory_fd))
    if actual_names != expected_names:
        raise RuntimeError(drift)
    for name in sorted(actual_names):
        relative = f"{prefix}/{name}" if prefix else name
        expected = entries.get(relative)
        if expected is None:
            raise RuntimeError(f"{drift}: {relative}")
        yield name, relative, expected, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _acquire_directory_leases(
    directory_fd: int,
    entries: dict[str, SourceEntry],
    mount_id: int,
    lease_stack: contextlib.ExitStack,
    leased_files: dict[str, int],
    prefix: str = "",
) -> None:
    """Hold every descendant file's lease on *lease_stack* until the whole tree is gone.

    Only a content-bound claim takes this route, so every file has a hash here;
    an identity-only one is leased per unlink instead.
    """
    for name, relative, expected, current in _claimed_children(directory_fd, entries, prefix, _EXCLUSION_DRIFT):
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                if _identity_for_fd(child_fd) != expected["identity"]:
                    raise RuntimeError(f"Recovery source changed before writer exclusion: {relative}")
                _acquire_directory_leases(child_fd, entries, mount_id, lease_stack, leased_files, relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = lease_stack.enter_context(_leased_regular(directory_fd, name, relative))
            _require_mount_id(file_fd, mount_id, relative)
            _require_entry_matches_fd(relative, current, _identity_for_fd(file_fd))
            _require_file_claim(
                relative,
                file_fd,
                expected["identity"],
                expected.get("sha256"),
                claimed=False,
                content_bound=True,
            )
            leased_files[relative] = file_fd
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")


def _delete_claimed_directory(
    directory_fd: int,
    entries: dict[str, SourceEntry],
    mount_id: int,
    prefix: str = "",
    *,
    leased_files: dict[str, int],
    content_bound: bool,
    progress: _RemovalProgress,
) -> None:
    """Unlink every claimed file, each re-proved under writer exclusion held across its own unlink.

    Under a content-bound claim that exclusion was taken over the whole tree
    before this pass began; under an identity-only one each file is leased here
    and released once it is gone, which is what lets a tree larger than the
    descriptor limit be removed at all.
    """
    for name, relative, expected, current in _claimed_children(directory_fd, entries, prefix, _CONSUMED_DRIFT):
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                if _identity_for_fd(child_fd) != expected["identity"]:
                    raise RuntimeError(f"Recovery source changed while it was consumed: {relative}")
                _delete_claimed_directory(
                    child_fd,
                    entries,
                    mount_id,
                    relative,
                    leased_files=leased_files,
                    content_bound=content_bound,
                    progress=progress,
                )
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(current.st_mode):
            with contextlib.ExitStack() as unlink_stack:
                file_fd = (
                    leased_files[relative]
                    if content_bound
                    else unlink_stack.enter_context(_leased_regular(directory_fd, name, relative))
                )
                _require_mount_id(file_fd, mount_id, relative)
                _require_entry_matches_fd(relative, current, _identity_for_fd(file_fd))
                _require_file_claim(
                    relative,
                    file_fd,
                    expected["identity"],
                    expected.get("sha256"),
                    claimed=False,
                    content_bound=content_bound,
                )
                os.unlink(name, dir_fd=directory_fd)
            progress.tick()
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")


def _validate_claimed_directory(
    directory_fd: int,
    entries: dict[str, SourceEntry],
    mount_id: int,
    prefix: str = "",
    *,
    leased_files: dict[str, int],
    content_bound: bool,
) -> None:
    """Refuse the whole subtree before any unlink if it drifted since it was claimed.

    Under a content-bound claim every regular file is already held under writer
    exclusion and was hashed while its lease was taken, so the only remaining
    change this pass can see is a directory-entry level one — a rename, unlink,
    or create at a claimed name. Content is re-hashed once more per file
    immediately before its unlink in :func:`_delete_claimed_directory`, which is
    the authoritative pre-mutation validation.

    Under an identity-only claim nothing is leased yet, so this pass takes and
    drops each file's lease in turn. That keeps the guarantee that matters most:
    a writer holding any file in the tree, or any identity drift, refuses the
    whole subtree with nothing deleted. Only a writer that arrives *during* the
    unlink loop can reach a partial removal, and that is reported as one.
    """
    for name, relative, expected, current in _claimed_children(directory_fd, entries, prefix, _CONSUMED_DRIFT):
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                if _identity_for_fd(child_fd) != expected["identity"]:
                    raise RuntimeError(f"Recovery source changed while it was consumed: {relative}")
                _validate_claimed_directory(
                    child_fd,
                    entries,
                    mount_id,
                    relative,
                    leased_files=leased_files,
                    content_bound=content_bound,
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            with contextlib.ExitStack() as probe:
                file_fd = (
                    leased_files[relative]
                    if content_bound
                    else probe.enter_context(_leased_regular(directory_fd, name, relative))
                )
                _require_mount_id(file_fd, mount_id, relative)
                _require_entry_matches_fd(relative, current, _identity_for_fd(file_fd))
                if _identity_for_fd(file_fd) != expected["identity"]:
                    raise RuntimeError(f"Recovery source changed while it was consumed: {relative}")
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")
