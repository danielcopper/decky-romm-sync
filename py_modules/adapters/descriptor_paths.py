"""Descriptor-relative exact-identity mutation helpers for recovery-backed cleanup."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.prune import MutationOutcome, SourceClaim, SourceEntry, SourceIdentity


def identity_for_stat(value: os.stat_result) -> SourceIdentity:
    return {
        "exists": True,
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": value.st_mode,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def missing_identity() -> SourceIdentity:
    return {"exists": False, "device": 0, "inode": 0, "mode": 0, "size": 0, "mtime_ns": 0, "ctime_ns": 0}


def stat_beneath(path: str, safe_root: str) -> os.stat_result | None:
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        return None
    try:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
    finally:
        os.close(parent_fd)


def claim_source(path: str, safe_root: str) -> SourceClaim:
    """Capture a complete no-follow identity claim for one source tree."""
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        return _claim(path, safe_root, missing_identity(), {})
    try:
        current = _stat_name(parent_fd, name)
        if current is None:
            return _claim(path, safe_root, missing_identity(), {})
        identity = identity_for_stat(current)
        if stat.S_ISREG(current.st_mode):
            return _claim(path, safe_root, identity, {})
        if not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"Recovery source has unsupported type: {path}")
        directory_fd = _open_child_directory(parent_fd, name)
        try:
            if identity_for_stat(os.fstat(directory_fd)) != identity:
                raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
            entries = _inventory_directory(directory_fd)
        finally:
            os.close(directory_fd)
        return _claim(path, safe_root, identity, entries)
    finally:
        os.close(parent_fd)


def remove_claimed(path: str, safe_root: str, claim: SourceClaim) -> MutationOutcome:
    """Claim, revalidate, and durably remove exactly one source tree."""
    _require_claim_shape(path, safe_root, claim)
    expected = claim["source_identity"]
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        if expected["exists"]:
            raise RuntimeError(f"Recovery source disappeared after sealing: {path}") from None
        return _outcome(success=True, changed=False, ambiguous=False, message="Source was already absent")
    try:
        current = _stat_name(parent_fd, name)
        _require_identity(path, current, expected)
        if current is None:
            return _outcome(success=True, changed=False, ambiguous=False, message="Source was already absent")
        temporary = f".{name}.romm-prune-{current.st_ino}"
        if _stat_name(parent_fd, temporary) is not None:
            raise FileExistsError(f"Prune staging entry already exists: {temporary}")
        os.rename(name, temporary, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        claimed = _stat_name(parent_fd, temporary)
        try:
            _require_claimed_identity(path, claimed, expected)
            if stat.S_ISDIR(current.st_mode):
                directory_fd = _open_child_directory(parent_fd, temporary)
                try:
                    actual_entries = _inventory_directory(directory_fd)
                finally:
                    os.close(directory_fd)
                if actual_entries != claim["entries"]:
                    raise RuntimeError(f"Recovery source subtree changed after sealing: {path}")
            elif claim["entries"]:
                raise ValueError(f"Regular-file recovery claim has descendants: {path}")
        except Exception:
            try:
                _restore_claim(parent_fd, name, temporary)
            except Exception as restore_exc:
                return _outcome(
                    success=False,
                    changed=True,
                    ambiguous=True,
                    message=f"Source validation failed and rollback was uncertain: {restore_exc}",
                )
            raise
        try:
            if stat.S_ISDIR(current.st_mode):
                shutil.rmtree(temporary, dir_fd=parent_fd)
            else:
                os.unlink(temporary, dir_fd=parent_fd)
        except Exception as exc:
            return _outcome(
                success=False,
                changed=True,
                ambiguous=True,
                message=f"Source removal stopped after the source was claimed: {exc}",
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


def remove_exact(path: str, safe_root: str, expected: SourceIdentity) -> MutationOutcome:
    """Compatibility wrapper for an exact non-directory identity."""
    return remove_claimed(path, safe_root, _claim(path, safe_root, expected, {}))


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
            return _outcome(success=True, changed=False, ambiguous=False, message="Source was already absent")
        if stat.S_ISDIR(current.st_mode):
            directory_fd = _open_child_directory(source_fd, source_name)
            try:
                if _inventory_directory(directory_fd) != claim["entries"]:
                    raise RuntimeError(f"Recovery source subtree changed after sealing: {src}")
            finally:
                os.close(directory_fd)
        elif claim["entries"]:
            raise ValueError(f"Regular-file recovery claim has descendants: {src}")
        if _stat_name(destination_fd, destination_name) is not None:
            raise FileExistsError(f"Recovery destination already exists: {dst}")
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )
        claimed = _stat_name(destination_fd, destination_name)
        try:
            _require_claimed_identity(src, claimed, expected)
        except Exception:
            try:
                if _stat_name(source_fd, source_name) is None:
                    os.rename(
                        destination_name,
                        source_name,
                        src_dir_fd=destination_fd,
                        dst_dir_fd=source_fd,
                    )
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


def rename_exact(src: str, dst: str, safe_root: str, expected: SourceIdentity) -> MutationOutcome:
    """Compatibility wrapper for an exact non-directory identity."""
    return rename_claimed(src, dst, safe_root, _claim(src, safe_root, expected, {}))


def ensure_directory(path: str, safe_root: str, mode: int = 0o700) -> None:
    """Create and open each directory component beneath an anchored root."""
    parts = _relative_parts(path, safe_root)
    fd = _open_directory(safe_root, safe_root)
    try:
        for component in parts:
            try:
                next_fd = _open_child_directory(fd, component)
            except FileNotFoundError:
                os.mkdir(component, mode, dir_fd=fd)
                os.fsync(fd)
                next_fd = _open_child_directory(fd, component)
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
    try:
        for component in parts:
            next_fd = os.open(component, flags, dir_fd=fd)
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


def _inventory_directory(directory_fd: int, prefix: str = "") -> dict[str, SourceEntry]:
    entries: dict[str, SourceEntry] = {}
    for name in sorted(os.listdir(directory_fd)):
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        identity = identity_for_stat(current)
        if stat.S_ISDIR(current.st_mode):
            entries[relative] = {"identity": identity}
            child_fd = _open_child_directory(directory_fd, name)
            try:
                if identity_for_stat(os.fstat(child_fd)) != identity:
                    raise RuntimeError(f"Recovery source changed while inventorying: {relative}")
                entries.update(_inventory_directory(child_fd, relative))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                if identity_for_stat(os.fstat(file_fd)) != identity:
                    raise RuntimeError(f"Recovery source changed while inventorying: {relative}")
                digest = _sha256_fd(file_fd)
                if identity_for_stat(os.fstat(file_fd)) != identity:
                    raise RuntimeError(f"Recovery source changed while inventorying: {relative}")
                entries[relative] = {"identity": identity, "sha256": digest}
            finally:
                os.close(file_fd)
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")
    return entries


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, 1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def _claim(path: str, safe_root: str, identity: SourceIdentity, entries: dict[str, SourceEntry]) -> SourceClaim:
    return {
        "source_path": path,
        "safe_root": safe_root,
        "source_identity": identity,
        "entries": entries,
    }


def _require_claim_shape(path: str, safe_root: str, claim: SourceClaim) -> None:
    if claim["source_path"] != path or claim["safe_root"] != safe_root:
        raise ValueError(f"Source claim does not match its mutation target: {path}")


def _restore_claim(parent_fd: int, name: str, temporary: str) -> None:
    if _stat_name(parent_fd, name) is not None:
        raise RuntimeError(f"Cannot restore claimed source because its path was replaced: {name}")
    os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.fsync(parent_fd)


def _outcome(*, success: bool, changed: bool, ambiguous: bool, message: str) -> MutationOutcome:
    return {"success": success, "changed": changed, "ambiguous": ambiguous, "message": message}


def _require_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    if not expected["exists"]:
        if current is not None:
            raise RuntimeError(f"Recovery source appeared after sealing: {path}")
        return
    if current is None:
        raise RuntimeError(f"Recovery source disappeared after sealing: {path}")
    actual = identity_for_stat(current)
    if actual != expected:
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")


def _require_claimed_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    if current is None or not expected["exists"]:
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
    actual = identity_for_stat(current)
    stable_fields = ("device", "inode", "mode", "size", "mtime_ns")
    if any(actual[field] != expected[field] for field in stable_fields):
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
