"""Verified, atomically sealed recovery bundles for destructive local cleanup."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
from typing import TYPE_CHECKING, Any

from adapters.descriptor_paths import claim_source, identity_for_stat, missing_identity, stat_beneath
from domain.prune import sanitize_package_name

if TYPE_CHECKING:
    from models.prune import RecoveryArtifact, SourceClaim, SourceEntry, SourceIdentity

_SAFE_BUNDLE_ID = re.compile(r"^[0-9TZ]+_[1-9][0-9]*_[A-Za-z0-9-]+$", re.ASCII)


class RecoveryBundleAdapter:
    """Single owner of recovery staging, verification, and atomic sealing."""

    def __init__(self, *, user_home: str, package_name: str, plugin_version: str) -> None:
        self._home = os.path.abspath(user_home)
        self._root = os.path.join(self._home, f"{sanitize_package_name(package_name)}-recovery")
        self._plugin_version = plugin_version

    def root(self) -> str:
        return self._root

    def free_bytes(self) -> int:
        descriptors = self._open_layout(create=True)
        try:
            stats = os.fstatvfs(descriptors[1])
            return stats.f_bavail * stats.f_frsize
        finally:
            self._close_layout(descriptors)

    def measure_path(self, path: str, safe_root: str) -> int:
        total = 0
        for file_path in self._regular_files(path, safe_root):
            fd = self._open_regular_beneath(file_path, safe_root)
            try:
                total += os.fstat(fd).st_size
            finally:
                os.close(fd)
        return total

    def validate_sources(self, bundle_path: str) -> bool:
        """Verify that every sealed source set and source byte stream is unchanged."""
        descriptors: tuple[int, int, int, int] | None = None
        bundle_fd: int | None = None
        try:
            descriptors, bundle_fd = self._open_bundle(bundle_path)
            bundle_anchor = self._fd_path(bundle_fd)
            seal = json.loads(self._read_beneath(os.path.join(bundle_anchor, "SEAL.json"), bundle_anchor))
            if not isinstance(seal, dict):
                return False
            if (
                seal.get("sealed") is not True
                or seal.get("bundle_id") != os.path.basename(bundle_path)
                or type(seal.get("file_count")) is not int
            ):
                return False
            checksum_bytes = self._read_beneath(os.path.join(bundle_anchor, "checksums.sha256"), bundle_anchor)
            if hashlib.sha256(checksum_bytes).hexdigest() != seal.get("checksums_sha256"):
                return False
            checksums: dict[str, str] = {}
            for raw_line in checksum_bytes.decode("utf-8").splitlines():
                digest, separator, relative = raw_line.partition("  ")
                if not separator or not relative or os.path.isabs(relative) or ".." in relative.split("/"):
                    return False
                checksums[relative] = digest
            for relative, digest in checksums.items():
                fd = self._open_regular_beneath(os.path.join(bundle_anchor, *relative.split("/")), bundle_anchor)
                try:
                    if self._sha256_fd(fd) != digest:
                        return False
                finally:
                    os.close(fd)
            manifest = json.loads(self._read_beneath(os.path.join(bundle_anchor, "manifest.json"), bundle_anchor))
            if not isinstance(manifest, dict):
                return False
            source_sets = manifest.get("source_sets")
            records = manifest.get("artifacts")
            if not isinstance(source_sets, list) or not isinstance(records, list):
                return False
            if seal["file_count"] != len(records):
                return False
            for source_set in source_sets:
                if not isinstance(source_set, dict):
                    return False
                source_path = source_set.get("source_path")
                safe_root = source_set.get("safe_root")
                files = source_set.get("files")
                raw_identity = source_set.get("source_identity")
                raw_entries = source_set.get("entries")
                if (
                    not isinstance(source_path, str)
                    or not isinstance(safe_root, str)
                    or not isinstance(files, list)
                    or not isinstance(raw_identity, dict)
                    or not isinstance(raw_entries, dict)
                ):
                    return False
                if self._regular_files(source_path, safe_root) != files:
                    return False
                expected_claim = self._decode_claim(source_path, safe_root, raw_identity, raw_entries)
                if claim_source(source_path, safe_root) != expected_claim:
                    return False
            for record in records:
                if not isinstance(record, dict):
                    return False
                source_path = record.get("source_path")
                safe_root = record.get("safe_root")
                digest = record.get("sha256")
                raw_identity = record.get("source_identity")
                if (
                    not isinstance(source_path, str)
                    or not isinstance(safe_root, str)
                    or not isinstance(digest, str)
                    or not isinstance(raw_identity, dict)
                ):
                    return False
                if claim_source(source_path, safe_root)["source_identity"] != self._decode_identity(raw_identity):
                    return False
                fd = self._open_regular_beneath(source_path, safe_root)
                try:
                    if self._sha256_fd(fd) != digest:
                        return False
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False
        finally:
            if bundle_fd is not None:
                os.close(bundle_fd)
            if descriptors is not None:
                self._close_layout(descriptors)
        return True

    def source_claims(self, bundle_path: str) -> dict[str, SourceClaim]:
        """Return every complete sealed source claim after full validation."""
        if not self.validate_sources(bundle_path):
            raise ValueError("Recovery bundle is not valid")
        descriptors, bundle_fd = self._open_bundle(bundle_path)
        try:
            anchor = self._fd_path(bundle_fd)
            manifest = json.loads(self._read_beneath(os.path.join(anchor, "manifest.json"), anchor))
        finally:
            os.close(bundle_fd)
            self._close_layout(descriptors)
        source_sets = manifest.get("source_sets") if isinstance(manifest, dict) else None
        if not isinstance(source_sets, list):
            raise ValueError("Recovery manifest source claims are missing")
        claims: dict[str, SourceClaim] = {}
        for item in source_sets:
            if not isinstance(item, dict):
                raise ValueError("Recovery manifest source claim is invalid")
            source_path = item.get("source_path")
            safe_root = item.get("safe_root")
            raw_identity = item.get("source_identity")
            raw_entries = item.get("entries")
            if (
                not isinstance(source_path, str)
                or not isinstance(safe_root, str)
                or not isinstance(raw_identity, dict)
                or not isinstance(raw_entries, dict)
            ):
                raise ValueError("Recovery manifest source claim is invalid")
            claims[source_path] = self._decode_claim(source_path, safe_root, raw_identity, raw_entries)
        return claims

    @classmethod
    def _read_beneath(cls, path: str, safe_root: str) -> bytes:
        fd = cls._open_regular_beneath(path, safe_root)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            blocks: list[bytes] = []
            while block := os.read(fd, 1024 * 1024):
                blocks.append(block)
            return b"".join(blocks)
        finally:
            os.close(fd)

    def seal_bundle(
        self,
        bundle_id: str,
        snapshot: dict[str, object],
        artifacts: list[RecoveryArtifact],
        readme: str,
        playtime_text: str,
    ) -> str:
        if _SAFE_BUNDLE_ID.fullmatch(bundle_id) is None:
            raise ValueError("unsafe recovery bundle id")
        bundles_parent = os.path.join(self._root, "bundles")
        staging_name = f".{bundle_id}.staging"
        sealed = os.path.join(bundles_parent, bundle_id)
        descriptors = self._open_layout(create=True)
        home_fd, root_fd, staging_parent_fd, bundles_parent_fd = descriptors
        staging_fd: int | None = None
        renamed = False
        try:
            if (
                self._stat_at(staging_parent_fd, staging_name) is not None
                or self._stat_at(bundles_parent_fd, bundle_id) is not None
            ):
                raise FileExistsError(f"Recovery bundle already exists: {bundle_id}")
            os.mkdir(staging_name, 0o700, dir_fd=staging_parent_fd)
            staging_fd = self._open_dir_at(staging_parent_fd, staging_name)
            staging_anchor = self._fd_path(staging_fd)
            free_bytes = self._free_bytes_fd(root_fd)
            records, source_sets, checksums = self._copy_artifacts(staging_anchor, artifacts, free_bytes)
            enriched = dict(snapshot)
            enriched["plugin_version"] = self._plugin_version
            enriched["bundle_id"] = bundle_id
            enriched["artifacts"] = records
            enriched["source_sets"] = source_sets
            self._write_rom_states(staging_anchor, enriched, checksums)
            self._write_verified_text(staging_anchor, "README.txt", readme, checksums)
            self._write_verified_text(staging_anchor, "playtime.txt", playtime_text, checksums)
            self._write_verified_text(
                staging_anchor,
                "manifest.json",
                json.dumps(enriched, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums,
            )
            checksum_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items()))
            self._write_verified_text(staging_anchor, "checksums.sha256", checksum_text, checksums=None)
            seal = {
                "bundle_id": bundle_id,
                "checksums_sha256": self._sha256(os.path.join(staging_anchor, "checksums.sha256")),
                "file_count": len(records),
                "sealed": True,
            }
            self._write_verified_text(
                staging_anchor,
                "SEAL.json",
                json.dumps(seal, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums=None,
            )
            os.fsync(staging_fd)
            os.rename(
                staging_name,
                bundle_id,
                src_dir_fd=staging_parent_fd,
                dst_dir_fd=bundles_parent_fd,
            )
            renamed = True
            try:
                os.fsync(staging_parent_fd)
                os.fsync(bundles_parent_fd)
                self._require_layout_attached(home_fd, root_fd, staging_parent_fd, bundles_parent_fd)
            except (OSError, ValueError) as exc:
                uncertain_name = bundle_id + ".durability-uncertain"
                with contextlib.suppress(OSError):
                    os.rename(
                        bundle_id,
                        uncertain_name,
                        src_dir_fd=bundles_parent_fd,
                        dst_dir_fd=bundles_parent_fd,
                    )
                    os.fsync(bundles_parent_fd)
                uncertain = os.path.join(bundles_parent, uncertain_name)
                raise OSError(f"Recovery bundle durability is uncertain: {uncertain}") from exc
            return sealed
        except BaseException:
            with contextlib.suppress(OSError):
                if renamed:
                    shutil.rmtree(bundle_id, dir_fd=bundles_parent_fd)
                else:
                    shutil.rmtree(staging_name, dir_fd=staging_parent_fd)
            raise
        finally:
            if staging_fd is not None:
                os.close(staging_fd)
            self._close_layout(descriptors)

    def _copy_artifacts(
        self, staging: str, artifacts: list[RecoveryArtifact], free_bytes: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, object]], dict[str, str]]:
        expanded: list[tuple[RecoveryArtifact, str]] = []
        source_sets: list[dict[str, object]] = []
        seen: set[str] = set()
        for artifact in artifacts:
            files = self._regular_files(artifact["source_path"], artifact["safe_root"])
            source_stat = stat_beneath(artifact["source_path"], artifact["safe_root"])
            source_sets.append(
                {
                    "source_path": artifact["source_path"],
                    "safe_root": artifact["safe_root"],
                    "files": files,
                    "kind": artifact["kind"],
                    "source_identity": identity_for_stat(source_stat)
                    if source_stat is not None
                    else missing_identity(),
                    **({"rom_id": artifact["rom_id"]} if "rom_id" in artifact else {}),
                }
            )
            for file_path in files:
                real = os.path.realpath(file_path)
                if real in seen:
                    continue
                seen.add(real)
                expanded.append((artifact, file_path))
        required = 0
        for artifact, path in expanded:
            fd = self._open_regular_beneath(path, artifact["safe_root"])
            try:
                required += os.fstat(fd).st_size
            finally:
                os.close(fd)
        if free_bytes < required:
            raise OSError(f"Insufficient recovery space: need {required} bytes")

        files_dir = os.path.join(staging, "files")
        os.mkdir(files_dir, 0o700)
        records: list[dict[str, Any]] = []
        checksums: dict[str, str] = {}
        for index, (artifact, source) in enumerate(expanded, start=1):
            relative = f"files/{index:06d}"
            destination = os.path.join(staging, relative)
            source_stat, source_hash = self._copy_opened_source(source, artifact["safe_root"], destination)
            os.chmod(destination, stat.S_IMODE(source_stat.st_mode))
            os.utime(destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns), follow_symlinks=False)
            destination_hash = self._sha256(destination)
            if source_hash != destination_hash:
                raise OSError(f"Recovery checksum mismatch for {source}")
            with open(destination, "rb") as copied:
                os.fsync(copied.fileno())
            checksums[relative] = destination_hash
            record: dict[str, Any] = {
                "kind": artifact["kind"],
                "source_path": source,
                "safe_root": artifact["safe_root"],
                "destination": relative,
                "size": source_stat.st_size,
                "mode": stat.S_IMODE(source_stat.st_mode),
                "mtime_ns": source_stat.st_mtime_ns,
                "sha256": destination_hash,
                "source_identity": identity_for_stat(source_stat),
            }
            if "rom_id" in artifact:
                record["rom_id"] = artifact["rom_id"]
            records.append(record)
        self._fsync_dir(files_dir)
        for source_set in source_sets:
            source_path = source_set["source_path"]
            safe_root = source_set["safe_root"]
            if not isinstance(source_path, str) or not isinstance(safe_root, str):
                raise ValueError("Recovery source set is invalid")
            claim = claim_source(source_path, safe_root)
            source_set["source_identity"] = claim["source_identity"]
            source_set["entries"] = claim["entries"]
        return records, source_sets, checksums

    @classmethod
    def _copy_opened_source(cls, source: str, safe_root: str, destination: str) -> tuple[os.stat_result, str]:
        source_fd = cls._open_regular_beneath(source, safe_root)
        try:
            before = os.fstat(source_fd)
            digest = hashlib.sha256()
            destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                while block := os.read(source_fd, 1024 * 1024):
                    digest.update(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_fd, view)
                        view = view[written:]
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            after = os.fstat(source_fd)
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if identity_after != identity_before:
                raise OSError(f"Recovery source changed while it was copied: {source}")
            return before, digest.hexdigest()
        finally:
            os.close(source_fd)

    @staticmethod
    def _regular_files(path: str, safe_root: str) -> list[str]:
        real_root = os.path.realpath(safe_root)
        real_path = os.path.realpath(path)
        try:
            contained = os.path.commonpath((real_root, real_path)) == real_root
        except ValueError as exc:
            raise ValueError(f"Recovery source is outside its safe root: {path}") from exc
        if not contained:
            raise ValueError(f"Recovery source is outside its safe root: {path}")
        if not os.path.lexists(path):
            return []
        if os.path.islink(path):
            raise ValueError(f"Recovery source may not be a symlink: {path}")
        if os.path.isfile(path):
            return [path]
        if not os.path.isdir(path):
            raise ValueError(f"Recovery source has unsupported type: {path}")
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for dirname in dirnames:
                candidate = os.path.join(dirpath, dirname)
                if os.path.islink(candidate):
                    raise ValueError(f"Recovery directory may not contain symlinks: {candidate}")
            for filename in filenames:
                candidate = os.path.join(dirpath, filename)
                if os.path.islink(candidate) or not os.path.isfile(candidate):
                    raise ValueError(f"Recovery directory contains a non-regular file: {candidate}")
                files.append(candidate)
        return sorted(files)

    @staticmethod
    def _open_regular_beneath(path: str, safe_root: str) -> int:
        absolute_root = os.path.abspath(safe_root)
        absolute_path = os.path.abspath(path)
        try:
            if os.path.commonpath((absolute_root, absolute_path)) != absolute_root:
                raise ValueError(f"Recovery source is outside its safe root: {path}")
        except ValueError as exc:
            raise ValueError(f"Recovery source is outside its safe root: {path}") from exc
        relative = os.path.relpath(absolute_path, absolute_root)
        if relative in {".", ".."} or relative.startswith(".." + os.sep):
            raise ValueError(f"Recovery source is not a file below its safe root: {path}")
        parts = relative.split(os.sep)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        canonical_root = os.path.realpath(absolute_root)
        directory_fd = os.open(canonical_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
        try:
            for component in parts[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=directory_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                os.close(file_fd)
                raise ValueError(f"Recovery source is not a regular file: {path}")
            return file_fd
        finally:
            os.close(directory_fd)

    def _write_rom_states(self, staging: str, snapshot: dict[str, object], checksums: dict[str, str]) -> None:
        rows = snapshot.get("roms")
        if not isinstance(rows, list):
            return
        roms_dir = os.path.join(staging, "roms")
        os.mkdir(roms_dir, 0o700)
        for row in rows:
            if not isinstance(row, dict) or type(row.get("rom_id")) is not int or row["rom_id"] <= 0:
                raise ValueError("Recovery snapshot contains an invalid ROM id")
            rom_id = int(row["rom_id"])
            directory = os.path.join(roms_dir, str(rom_id))
            os.mkdir(directory, 0o700)
            per_rom = {
                key: [item for item in value if isinstance(item, dict) and item.get("rom_id") == rom_id]
                if isinstance(value, list)
                else value
                for key, value in snapshot.items()
                if key in {"roms", "installs", "metadata", "save_sync", "playtime"}
            }
            relative = f"roms/{rom_id}/state.json"
            self._write_verified_text(
                staging,
                relative,
                json.dumps(per_rom, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums,
            )
            self._fsync_dir(directory)
        self._fsync_dir(roms_dir)

    def _write_verified_text(self, staging: str, relative: str, content: str, checksums: dict[str, str] | None) -> None:
        path = os.path.join(staging, relative)
        with open(path, "x", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if checksums is not None:
            checksums[relative] = self._sha256(path)

    @staticmethod
    def _sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _sha256_fd(fd: int) -> str:
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()

    def _open_layout(self, *, create: bool) -> tuple[int, int, int, int]:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if create:
            os.makedirs(self._home, exist_ok=True)
        home_fd = os.open(self._home, flags)
        root_fd: int | None = None
        staging_fd: int | None = None
        bundles_fd: int | None = None
        try:
            root_name = os.path.basename(self._root)
            root_fd = self._open_or_create_dir(home_fd, root_name, create=create)
            staging_fd = self._open_or_create_dir(root_fd, "staging", create=create)
            bundles_fd = self._open_or_create_dir(root_fd, "bundles", create=create)
            return home_fd, root_fd, staging_fd, bundles_fd
        except BaseException:
            for fd in (bundles_fd, staging_fd, root_fd, home_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise

    @staticmethod
    def _close_layout(descriptors: tuple[int, int, int, int]) -> None:
        for fd in reversed(descriptors):
            os.close(fd)

    @staticmethod
    def _open_or_create_dir(parent_fd: int, name: str, *, create: bool) -> int:
        try:
            return RecoveryBundleAdapter._open_dir_at(parent_fd, name)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return RecoveryBundleAdapter._open_dir_at(parent_fd, name)
        except OSError as exc:
            raise ValueError(f"Recovery directory is not a trusted directory: {name}") from exc

    @staticmethod
    def _open_dir_at(parent_fd: int, name: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(name, flags, dir_fd=parent_fd)

    def _open_bundle(self, bundle_path: str) -> tuple[tuple[int, int, int, int], int]:
        expected_parent = os.path.join(self._root, "bundles")
        absolute = os.path.abspath(bundle_path)
        bundle_id = os.path.basename(absolute)
        if os.path.dirname(absolute) != expected_parent or _SAFE_BUNDLE_ID.fullmatch(bundle_id) is None:
            raise ValueError("Recovery bundle path is outside the anchored bundle directory")
        descriptors = self._open_layout(create=False)
        try:
            self._require_layout_attached(*descriptors)
            return descriptors, self._open_dir_at(descriptors[3], bundle_id)
        except BaseException:
            self._close_layout(descriptors)
            raise

    def _require_layout_attached(
        self,
        home_fd: int,
        root_fd: int,
        staging_fd: int,
        bundles_fd: int,
    ) -> None:
        root_name = os.path.basename(self._root)
        checks = (
            (home_fd, root_name, root_fd),
            (root_fd, "staging", staging_fd),
            (root_fd, "bundles", bundles_fd),
        )
        for parent_fd, name, held_fd in checks:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            held = os.fstat(held_fd)
            if (current.st_dev, current.st_ino, current.st_mode) != (held.st_dev, held.st_ino, held.st_mode):
                raise ValueError("Recovery directory identity changed while the bundle was open")

    @staticmethod
    def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _fd_path(fd: int) -> str:
        return f"/proc/self/fd/{fd}"

    @staticmethod
    def _free_bytes_fd(fd: int) -> int:
        stats = os.fstatvfs(fd)
        return stats.f_bavail * stats.f_frsize

    @staticmethod
    def _decode_identity(raw: dict[str, object]) -> SourceIdentity:
        def integer(key: str) -> int:
            value = raw.get(key, 0)
            if type(value) is not int:
                raise ValueError(f"Recovery source identity field is invalid: {key}")
            return value

        return {
            "exists": raw.get("exists") is True,
            "device": integer("device"),
            "inode": integer("inode"),
            "mode": integer("mode"),
            "size": integer("size"),
            "mtime_ns": integer("mtime_ns"),
            "ctime_ns": integer("ctime_ns"),
        }

    @classmethod
    def _decode_claim(
        cls,
        source_path: str,
        safe_root: str,
        raw_identity: dict[str, object],
        raw_entries: dict[str, object],
    ) -> SourceClaim:
        entries: dict[str, SourceEntry] = {}
        for relative, raw_entry in raw_entries.items():
            if not isinstance(raw_entry, dict):
                raise ValueError("Recovery source claim entry is invalid")
            identity = raw_entry.get("identity")
            if not isinstance(identity, dict):
                raise ValueError("Recovery source claim identity is invalid")
            entry: SourceEntry = {"identity": cls._decode_identity(identity)}
            digest = raw_entry.get("sha256")
            if digest is not None:
                if not isinstance(digest, str):
                    raise ValueError("Recovery source claim checksum is invalid")
                entry["sha256"] = digest
            entries[relative] = entry
        return {
            "source_path": source_path,
            "safe_root": safe_root,
            "source_identity": cls._decode_identity(raw_identity),
            "entries": entries,
        }

    def _ensure_dir(self, path: str) -> None:
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isdir(path):
                raise ValueError(f"Recovery directory is not a trusted directory: {path}")
            return
        os.mkdir(path, 0o700)
        parent = os.path.dirname(path)
        if parent:
            self._fsync_dir(parent)

    @staticmethod
    def _fsync_dir(path: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                return
            raise
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
        finally:
            os.close(fd)
