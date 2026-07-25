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

from domain.prune import sanitize_package_name

if TYPE_CHECKING:
    from models.prune import RecoveryArtifact

_SAFE_BUNDLE_ID = re.compile(r"^[0-9TZ]+_[1-9][0-9]*_[A-Za-z0-9-]+$", re.ASCII)


class RecoveryBundleAdapter:
    """Single owner of recovery staging, verification, and atomic sealing."""

    def __init__(self, *, user_home: str, package_name: str, plugin_version: str) -> None:
        self._root = os.path.join(user_home, f"{sanitize_package_name(package_name)}-recovery")
        self._plugin_version = plugin_version

    def root(self) -> str:
        return self._root

    def free_bytes(self) -> int:
        os.makedirs(os.path.dirname(self._root), exist_ok=True)
        self._ensure_dir(self._root)
        return shutil.disk_usage(self._root).free

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
        bundles_parent = os.path.join(self._root, "bundles")
        expected_parent = os.path.realpath(bundles_parent)
        if os.path.dirname(os.path.realpath(bundle_path)) != expected_parent or os.path.islink(bundle_path):
            return False
        try:
            seal = json.loads(self._read_beneath(os.path.join(bundle_path, "SEAL.json"), bundle_path))
            if not isinstance(seal, dict):
                return False
            checksum_bytes = self._read_beneath(os.path.join(bundle_path, "checksums.sha256"), bundle_path)
            if hashlib.sha256(checksum_bytes).hexdigest() != seal.get("checksums_sha256"):
                return False
            checksums: dict[str, str] = {}
            for raw_line in checksum_bytes.decode("utf-8").splitlines():
                digest, separator, relative = raw_line.partition("  ")
                if not separator or not relative or os.path.isabs(relative) or ".." in relative.split("/"):
                    return False
                checksums[relative] = digest
            for relative, digest in checksums.items():
                fd = self._open_regular_beneath(os.path.join(bundle_path, *relative.split("/")), bundle_path)
                try:
                    if self._sha256_fd(fd) != digest:
                        return False
                finally:
                    os.close(fd)
            manifest = json.loads(self._read_beneath(os.path.join(bundle_path, "manifest.json"), bundle_path))
            if not isinstance(manifest, dict):
                return False
            source_sets = manifest.get("source_sets")
            records = manifest.get("artifacts")
            if not isinstance(source_sets, list) or not isinstance(records, list):
                return False
            for source_set in source_sets:
                if not isinstance(source_set, dict):
                    return False
                source_path = source_set.get("source_path")
                safe_root = source_set.get("safe_root")
                files = source_set.get("files")
                if not isinstance(source_path, str) or not isinstance(safe_root, str) or not isinstance(files, list):
                    return False
                if self._regular_files(source_path, safe_root) != files:
                    return False
            for record in records:
                if not isinstance(record, dict):
                    return False
                source_path = record.get("source_path")
                safe_root = record.get("safe_root")
                digest = record.get("sha256")
                if not isinstance(source_path, str) or not isinstance(safe_root, str) or not isinstance(digest, str):
                    return False
                fd = self._open_regular_beneath(source_path, safe_root)
                try:
                    if self._sha256_fd(fd) != digest:
                        return False
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return True

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
        staging_parent = os.path.join(self._root, "staging")
        bundles_parent = os.path.join(self._root, "bundles")
        staging = os.path.join(staging_parent, f".{bundle_id}.staging")
        sealed = os.path.join(bundles_parent, bundle_id)
        self._ensure_dir(self._root)
        self._ensure_dir(staging_parent)
        self._ensure_dir(bundles_parent)
        if os.path.lexists(staging) or os.path.lexists(sealed):
            raise FileExistsError(f"Recovery bundle already exists: {bundle_id}")

        try:
            os.mkdir(staging, 0o700)
            records, source_sets, checksums = self._copy_artifacts(staging, artifacts)
            enriched = dict(snapshot)
            enriched["plugin_version"] = self._plugin_version
            enriched["bundle_id"] = bundle_id
            enriched["artifacts"] = records
            enriched["source_sets"] = source_sets
            self._write_rom_states(staging, enriched, checksums)
            self._write_verified_text(staging, "README.txt", readme, checksums)
            self._write_verified_text(staging, "playtime.txt", playtime_text, checksums)
            self._write_verified_text(
                staging,
                "manifest.json",
                json.dumps(enriched, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums,
            )
            checksum_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items()))
            self._write_verified_text(staging, "checksums.sha256", checksum_text, checksums=None)
            seal = {
                "bundle_id": bundle_id,
                "checksums_sha256": self._sha256(os.path.join(staging, "checksums.sha256")),
                "file_count": len(records),
                "sealed": True,
            }
            self._write_verified_text(
                staging,
                "SEAL.json",
                json.dumps(seal, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums=None,
            )
            self._fsync_dir(staging)
            os.replace(staging, sealed)
            self._fsync_dir(bundles_parent)
            return sealed
        except BaseException:
            with contextlib.suppress(OSError):
                shutil.rmtree(staging)
            raise

    def _copy_artifacts(
        self, staging: str, artifacts: list[RecoveryArtifact]
    ) -> tuple[list[dict[str, Any]], list[dict[str, object]], dict[str, str]]:
        expanded: list[tuple[RecoveryArtifact, str]] = []
        source_sets: list[dict[str, object]] = []
        seen: set[str] = set()
        for artifact in artifacts:
            files = self._regular_files(artifact["source_path"], artifact["safe_root"])
            source_sets.append(
                {
                    "source_path": artifact["source_path"],
                    "safe_root": artifact["safe_root"],
                    "files": files,
                    "kind": artifact["kind"],
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
        if self.free_bytes() < required:
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
            }
            if "rom_id" in artifact:
                record["rom_id"] = artifact["rom_id"]
            records.append(record)
        self._fsync_dir(files_dir)
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
        real_root = os.path.realpath(safe_root)
        real_path = os.path.realpath(path)
        try:
            if os.path.commonpath((real_root, real_path)) != real_root:
                raise ValueError(f"Recovery source is outside its safe root: {path}")
        except ValueError as exc:
            raise ValueError(f"Recovery source is outside its safe root: {path}") from exc
        relative = os.path.relpath(real_path, real_root)
        if relative in {".", ".."} or relative.startswith(".." + os.sep):
            raise ValueError(f"Recovery source is not a file below its safe root: {path}")
        parts = relative.split(os.sep)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(real_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
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

    @staticmethod
    def _ensure_dir(path: str) -> None:
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isdir(path):
                raise ValueError(f"Recovery directory is not a trusted directory: {path}")
            return
        os.mkdir(path, 0o700)

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
