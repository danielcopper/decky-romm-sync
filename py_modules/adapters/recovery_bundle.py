"""Verified, atomically sealed recovery bundles for destructive local cleanup."""

from __future__ import annotations

import contextlib
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
        os.makedirs(self._root, exist_ok=True)
        return shutil.disk_usage(self._root).free

    def measure_path(self, path: str, safe_root: str) -> int:
        files = self._regular_files(path, safe_root)
        return sum(os.stat(file_path, follow_symlinks=False).st_size for file_path in files)

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
        os.makedirs(staging_parent, exist_ok=True)
        os.makedirs(bundles_parent, exist_ok=True)
        if os.path.exists(staging) or os.path.exists(sealed):
            raise FileExistsError(f"Recovery bundle already exists: {bundle_id}")

        try:
            os.mkdir(staging, 0o700)
            records, checksums = self._copy_artifacts(staging, artifacts)
            enriched = dict(snapshot)
            enriched["plugin_version"] = self._plugin_version
            enriched["bundle_id"] = bundle_id
            enriched["artifacts"] = records
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
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        expanded: list[tuple[RecoveryArtifact, str]] = []
        seen: set[str] = set()
        for artifact in artifacts:
            for file_path in self._regular_files(artifact["source_path"], artifact["safe_root"]):
                real = os.path.realpath(file_path)
                if real in seen:
                    continue
                seen.add(real)
                expanded.append((artifact, real))
        required = sum(os.stat(path, follow_symlinks=False).st_size for _, path in expanded)
        if self.free_bytes() < required:
            raise OSError(f"Insufficient recovery space: need {required} bytes")

        files_dir = os.path.join(staging, "files")
        os.mkdir(files_dir, 0o700)
        records: list[dict[str, Any]] = []
        checksums: dict[str, str] = {}
        for index, (artifact, source) in enumerate(expanded, start=1):
            relative = f"files/{index:06d}"
            destination = os.path.join(staging, relative)
            source_stat = os.stat(source, follow_symlinks=False)
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError(f"Recovery source is not a regular file: {source}")
            shutil.copyfile(source, destination, follow_symlinks=False)
            os.chmod(destination, stat.S_IMODE(source_stat.st_mode))
            os.utime(destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns), follow_symlinks=False)
            source_hash = self._sha256(source)
            destination_hash = self._sha256(destination)
            if source_hash != destination_hash:
                raise OSError(f"Recovery checksum mismatch for {source}")
            with open(destination, "rb") as copied:
                os.fsync(copied.fileno())
            checksums[relative] = destination_hash
            record: dict[str, Any] = {
                "kind": artifact["kind"],
                "source_path": source,
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
        return records, checksums

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
    def _fsync_dir(path: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
