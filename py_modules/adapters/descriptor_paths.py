"""Descriptor-relative exact-identity mutation helpers for recovery-backed cleanup."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.prune import MutationOutcome, SourceClaim, SourceEntry, SourceIdentity


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
        return _claim(path, safe_root, missing_identity(), None, {})
    try:
        current = _stat_name(parent_fd, name)
        if current is None:
            return _claim(path, safe_root, missing_identity(), None, {})
        if stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(parent_fd, name)
            try:
                _require_same_mount(parent_fd, file_fd, path)
                identity = _identity_for_fd(file_fd)
                _require_entry_matches_fd(path, current, identity)
                digest = _sha256_fd(file_fd)
                if _identity_for_fd(file_fd) != identity:
                    raise RuntimeError(f"Recovery source changed while it was claimed: {path}")
            finally:
                os.close(file_fd)
            return _claim(path, safe_root, identity, digest, {})
        if stat.S_ISLNK(current.st_mode):
            raise ValueError(f"Recovery source may not be a symlink: {path}")
        if not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"Recovery source has unsupported type: {path}")
        directory_fd = _open_child_directory(parent_fd, name)
        try:
            _require_same_mount(parent_fd, directory_fd, path)
            identity = _identity_for_fd(directory_fd)
            _require_entry_matches_fd(path, current, identity)
            entries = _inventory_directory(directory_fd, identity["mount_id"])
        finally:
            os.close(directory_fd)
        return _claim(path, safe_root, identity, None, entries)
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
                    _require_same_mount(parent_fd, directory_fd, path)
                    actual_entries = _inventory_directory(directory_fd, expected["mount_id"])
                finally:
                    os.close(directory_fd)
                if actual_entries != claim["entries"]:
                    raise RuntimeError(f"Recovery source subtree changed after sealing: {path}")
                directory_fd = _open_child_directory(parent_fd, temporary)
                try:
                    _validate_claimed_directory(directory_fd, claim["entries"], expected["mount_id"])
                finally:
                    os.close(directory_fd)
            elif claim["entries"]:
                raise ValueError(f"Regular-file recovery claim has descendants: {path}")
            else:
                file_fd = _open_child_regular(parent_fd, temporary)
                try:
                    _require_same_mount(parent_fd, file_fd, path)
                    _require_file_claim(path, file_fd, expected, claim["sha256"], claimed=True)
                finally:
                    os.close(file_fd)
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
                directory_fd = _open_child_directory(parent_fd, temporary)
                try:
                    _delete_claimed_directory(directory_fd, claim["entries"], expected["mount_id"])
                finally:
                    os.close(directory_fd)
                os.rmdir(temporary, dir_fd=parent_fd)
            else:
                file_fd = _open_child_regular(parent_fd, temporary)
                try:
                    _require_file_claim(path, file_fd, expected, claim["sha256"], claimed=True)
                    os.unlink(temporary, dir_fd=parent_fd)
                finally:
                    os.close(file_fd)
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
    try:
        claim = claim_source(path, safe_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}") from exc
    _require_compatible_expected(path, claim["source_identity"], expected)
    return remove_claimed(path, safe_root, claim)


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
                _require_same_mount(source_fd, directory_fd, src)
                if _inventory_directory(directory_fd, expected["mount_id"]) != claim["entries"]:
                    raise RuntimeError(f"Recovery source subtree changed after sealing: {src}")
            finally:
                os.close(directory_fd)
        elif claim["entries"]:
            raise ValueError(f"Regular-file recovery claim has descendants: {src}")
        else:
            file_fd = _open_child_regular(source_fd, source_name)
            try:
                _require_same_mount(source_fd, file_fd, src)
                _require_file_claim(src, file_fd, expected, claim["sha256"], claimed=False)
            finally:
                os.close(file_fd)
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
            if stat.S_ISDIR(current.st_mode):
                directory_fd = _open_child_directory(destination_fd, destination_name)
                try:
                    _require_same_mount(destination_fd, directory_fd, src)
                    if _inventory_directory(directory_fd, expected["mount_id"]) != claim["entries"]:
                        raise RuntimeError(f"Recovery source subtree changed while it was renamed: {src}")
                finally:
                    os.close(directory_fd)
            else:
                file_fd = _open_child_regular(destination_fd, destination_name)
                try:
                    _require_same_mount(destination_fd, file_fd, src)
                    _require_file_claim(src, file_fd, expected, claim["sha256"], claimed=True)
                finally:
                    os.close(file_fd)
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
    try:
        claim = claim_source(src, safe_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Recovery source identity changed after sealing: {src}") from exc
    _require_compatible_expected(src, claim["source_identity"], expected)
    return rename_claimed(src, dst, safe_root, claim)


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


def _inventory_directory(directory_fd: int, mount_id: int, prefix: str = "") -> dict[str, SourceEntry]:
    entries: dict[str, SourceEntry] = {}
    for name in sorted(os.listdir(directory_fd)):
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                identity = _identity_for_fd(child_fd)
                _require_entry_matches_fd(relative, current, identity)
                entries[relative] = {"identity": identity}
                entries.update(_inventory_directory(child_fd, mount_id, relative))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(directory_fd, name)
            try:
                _require_mount_id(file_fd, mount_id, relative)
                identity = _identity_for_fd(file_fd)
                _require_entry_matches_fd(relative, current, identity)
                digest = _sha256_fd(file_fd)
                if _identity_for_fd(file_fd) != identity:
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


def _claim(
    path: str,
    safe_root: str,
    identity: SourceIdentity,
    sha256: str | None,
    entries: dict[str, SourceEntry],
) -> SourceClaim:
    return {
        "source_path": path,
        "safe_root": safe_root,
        "source_identity": identity,
        "sha256": sha256,
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
    actual = identity_for_stat(current, expected["mount_id"])
    fields = ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
    if any(actual[field] != expected[field] for field in fields):
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")


def _require_claimed_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    if current is None or not expected["exists"]:
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
    actual = identity_for_stat(current, expected["mount_id"])
    stable_fields = ("mount_id", "device", "inode", "mode", "size", "mtime_ns")
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
) -> None:
    if expected_hash is None:
        raise ValueError(f"Regular-file recovery claim has no content hash: {path}")
    before = _identity_for_fd(fd)
    stable_fields = ("mount_id", "device", "inode", "mode", "size", "mtime_ns") if claimed else tuple(expected)
    if any(before[field] != expected[field] for field in stable_fields if field != "exists"):
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")
    if _sha256_fd(fd) != expected_hash or _identity_for_fd(fd) != before:
        raise RuntimeError(f"Recovery source content changed after sealing: {path}")


def _delete_claimed_directory(
    directory_fd: int, entries: dict[str, SourceEntry], mount_id: int, prefix: str = ""
) -> None:
    expected_names = {
        relative[len(prefix) + 1 :].split("/", 1)[0] if prefix else relative.split("/", 1)[0]
        for relative in entries
        if not prefix or relative.startswith(prefix + "/")
    }
    actual_names = set(os.listdir(directory_fd))
    if actual_names != expected_names:
        raise RuntimeError("Recovery source subtree changed while it was consumed")
    for name in sorted(actual_names):
        relative = f"{prefix}/{name}" if prefix else name
        expected = entries.get(relative)
        if expected is None:
            raise RuntimeError(f"Recovery source subtree changed while it was consumed: {relative}")
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                if _identity_for_fd(child_fd) != expected["identity"]:
                    raise RuntimeError(f"Recovery source changed while it was consumed: {relative}")
                _delete_claimed_directory(child_fd, entries, mount_id, relative)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(directory_fd, name)
            try:
                _require_mount_id(file_fd, mount_id, relative)
                _require_file_claim(relative, file_fd, expected["identity"], expected.get("sha256"), claimed=False)
                os.unlink(name, dir_fd=directory_fd)
            finally:
                os.close(file_fd)
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")


def _validate_claimed_directory(
    directory_fd: int, entries: dict[str, SourceEntry], mount_id: int, prefix: str = ""
) -> None:
    expected_names = {
        relative[len(prefix) + 1 :].split("/", 1)[0] if prefix else relative.split("/", 1)[0]
        for relative in entries
        if not prefix or relative.startswith(prefix + "/")
    }
    actual_names = set(os.listdir(directory_fd))
    if actual_names != expected_names:
        raise RuntimeError("Recovery source subtree changed while it was consumed")
    for name in sorted(actual_names):
        relative = f"{prefix}/{name}" if prefix else name
        expected = entries.get(relative)
        if expected is None:
            raise RuntimeError(f"Recovery source subtree changed while it was consumed: {relative}")
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(current.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                _require_mount_id(child_fd, mount_id, relative)
                if _identity_for_fd(child_fd) != expected["identity"]:
                    raise RuntimeError(f"Recovery source changed while it was consumed: {relative}")
                _validate_claimed_directory(child_fd, entries, mount_id, relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(current.st_mode):
            file_fd = _open_child_regular(directory_fd, name)
            try:
                _require_mount_id(file_fd, mount_id, relative)
                _require_file_claim(relative, file_fd, expected["identity"], expected.get("sha256"), claimed=False)
            finally:
                os.close(file_fd)
        else:
            raise ValueError(f"Recovery source contains unsupported entry: {relative}")


def _require_compatible_expected(path: str, actual: SourceIdentity, expected: SourceIdentity) -> None:
    fields = ("exists", "device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
    if any(actual[field] != expected[field] for field in fields):
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")
