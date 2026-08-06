"""Verified, atomically sealed recovery bundles for destructive local cleanup."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from typing import TYPE_CHECKING, Any

from adapters.descriptor_paths import (
    claim_source,
    identity_for_stat,
    measure_tree,
    mount_id_for_fd,
    raise_if_aborted,
    remove_current,
)
from domain.prune import render_bundle_readme, sanitize_package_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from models.prune import (
        RecoveryArtifact,
        SealedSourceClaims,
        SourceClaim,
        SourceEntry,
        SourceIdentity,
    )

    from domain.prune import BundleReadmeContext

_SAFE_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*_\d{4}-\d{2}-\d{2}_[A-Za-z0-9]{4,32}$", re.ASCII)
_ROOT_README_NAME = "README.txt"
_ROOT_README_TEXT = """decky-romm-sync recovery bundles
================================

This folder holds snapshots the RomM Sync plugin took immediately BEFORE it
deleted a game's local data, during "Clean Up Removed RomM Games".

  bundles/   one folder per cleaned-up game, named <game>_<date>_<id>.
             Each has its own README.txt explaining what it holds and how to
             put it back by hand.
  staging/   scratch space used while a bundle is being written. It is normally
             empty; anything left here is from a run that failed mid-write.

Nothing here is ever read back automatically — there is no restore button. The
plugin only writes to this folder, so it is safe to move, archive, or delete a
bundle once you are sure you no longer need it.
"""


class RecoveryBundleAdapter:
    """Single owner of recovery staging, verification, and atomic sealing."""

    def __init__(self, *, user_home: str, package_name: str, plugin_version: str) -> None:
        self._home = os.path.abspath(user_home)
        self._root = os.path.join(self._home, f"{sanitize_package_name(package_name)}-recovery")
        self._plugin_version = plugin_version

    def root(self) -> str:
        return self._root

    def free_bytes(self) -> int:
        """Report free bytes on the filesystem that holds — or would hold — the recovery root.

        Read-only: the ``staging`` / ``bundles`` layout is created by
        :meth:`seal_bundle`, never by a preview, so this walks up to the nearest
        existing ancestor instead of materializing the root.
        """
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        candidate = self._root
        while True:
            try:
                fd = os.open(candidate, flags)
            except FileNotFoundError:
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    raise
                candidate = parent
                continue
            except OSError as exc:
                raise ValueError(f"Recovery directory is not a trusted directory: {candidate}") from exc
            try:
                return self._free_bytes_fd(fd)
            finally:
                os.close(fd)

    def measure_path(self, path: str, safe_root: str) -> int:
        """Sum the recursive byte size of one source without reading its content."""
        return measure_tree(path, safe_root)

    def validate_sources(self, bundle_path: str, bundle_digest: str | None = None) -> bool:
        """Verify that every sealed source set and source byte stream is unchanged."""
        try:
            self._validated_source_claims(bundle_path, bundle_digest)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            return False
        return True

    def source_claims(self, bundle_path: str) -> SealedSourceClaims:
        """Decode claims from the same held descriptor used for full validation."""
        return self._validated_source_claims(bundle_path, None)

    def _validated_source_claims(self, bundle_path: str, expected_digest: str | None) -> SealedSourceClaims:
        descriptors, bundle_fd = self._open_bundle(bundle_path)
        try:
            anchor = self._fd_path(bundle_fd)
            seal_bytes = self._read_beneath(os.path.join(anchor, "SEAL.json"), anchor)
            checksum_bytes = self._read_beneath(os.path.join(anchor, "checksums.sha256"), anchor)
            manifest_bytes = self._read_beneath(os.path.join(anchor, "manifest.json"), anchor)
            seal, manifest = self._require_sealed_metadata(bundle_path, seal_bytes, checksum_bytes, manifest_bytes)
            bundle_digest = self._bundle_digest(bundle_fd, seal_bytes, checksum_bytes, manifest_bytes)
            if expected_digest is not None and bundle_digest != expected_digest:
                raise ValueError("Recovery bundle identity changed after claims were decoded")

            checksums = self._decode_checksums(checksum_bytes)
            self._verify_bundle_checksums(anchor, checksums)

            source_sets, records = self._require_manifest_shape(seal, manifest)
            claims, sealed_files = self._decode_source_claims(source_sets)
            self._require_records_match_claims(records, sealed_files)
            return {"claims": claims, "bundle_digest": bundle_digest}
        finally:
            os.close(bundle_fd)
            self._close_layout(descriptors)

    @staticmethod
    def _require_sealed_metadata(
        bundle_path: str, seal_bytes: bytes, checksum_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Decode the seal and manifest, refusing a bundle that was never sealed as this one."""
        seal = json.loads(seal_bytes)
        manifest = json.loads(manifest_bytes)
        if not isinstance(seal, dict) or not isinstance(manifest, dict):
            raise ValueError("Recovery bundle metadata is invalid")
        if (
            seal.get("sealed") is not True
            or seal.get("bundle_id") != os.path.basename(bundle_path)
            or type(seal.get("file_count")) is not int
            or hashlib.sha256(checksum_bytes).hexdigest() != seal.get("checksums_sha256")
        ):
            raise ValueError("Recovery bundle seal is invalid")
        return seal, manifest

    @staticmethod
    def _bundle_digest(bundle_fd: int, seal_bytes: bytes, checksum_bytes: bytes, manifest_bytes: bytes) -> str:
        """Fingerprint the held bundle directory together with the metadata just read from it."""
        bundle_stat = os.fstat(bundle_fd)
        digest_payload = {
            "device": bundle_stat.st_dev,
            "inode": bundle_stat.st_ino,
            "mode": bundle_stat.st_mode,
            "seal": hashlib.sha256(seal_bytes).hexdigest(),
            "checksums": hashlib.sha256(checksum_bytes).hexdigest(),
            "manifest": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        return hashlib.sha256(json.dumps(digest_payload, ensure_ascii=True, sort_keys=True).encode("ascii")).hexdigest()

    @staticmethod
    def _decode_checksums(checksum_bytes: bytes) -> dict[str, str]:
        """Read the sealed checksum list, refusing any entry that could point outside the bundle."""
        checksums: dict[str, str] = {}
        for raw_line in checksum_bytes.decode("utf-8").splitlines():
            digest, separator, relative = raw_line.partition("  ")
            if not separator or not relative or os.path.isabs(relative) or ".." in relative.split("/"):
                raise ValueError("Recovery checksum list is invalid")
            checksums[relative] = digest
        return checksums

    def _verify_bundle_checksums(self, anchor: str, checksums: dict[str, str]) -> None:
        """Re-hash every sealed file below the held bundle directory."""
        for relative, digest in checksums.items():
            fd = self._open_regular_beneath(os.path.join(anchor, *relative.split("/")), anchor)
            try:
                if self._sha256_fd(fd) != digest:
                    raise ValueError("Recovery bundle checksum mismatch")
            finally:
                os.close(fd)

    @staticmethod
    def _require_manifest_shape(seal: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[Any], list[Any]]:
        """Split the manifest into its source sets and artifact records, as the seal counted them."""
        source_sets = manifest.get("source_sets")
        records = manifest.get("artifacts")
        if not isinstance(source_sets, list) or not isinstance(records, list) or seal["file_count"] != len(records):
            raise ValueError("Recovery manifest is invalid")
        return source_sets, records

    def _decode_source_claims(
        self, source_sets: list[Any]
    ) -> tuple[dict[str, SourceClaim], dict[str, tuple[str, SourceIdentity, str]]]:
        """Decode each sealed claim and prove the source on disk still matches it exactly."""
        claims: dict[str, SourceClaim] = {}
        sealed_files: dict[str, tuple[str, SourceIdentity, str]] = {}
        for item in source_sets:
            if not isinstance(item, dict):
                raise ValueError("Recovery manifest source claim is invalid")
            source_path = item.get("source_path")
            safe_root = item.get("safe_root")
            files = item.get("files")
            raw_identity = item.get("source_identity")
            raw_sha256 = item.get("sha256")
            raw_entries = item.get("entries")
            if (
                not isinstance(source_path, str)
                or not isinstance(safe_root, str)
                or not isinstance(files, list)
                or not isinstance(raw_identity, dict)
                or (raw_sha256 is not None and not isinstance(raw_sha256, str))
                or not isinstance(raw_entries, dict)
            ):
                raise ValueError("Recovery manifest source claim is invalid")
            claim = self._decode_claim(source_path, safe_root, raw_identity, raw_sha256, raw_entries)
            if self._files_for_claim(claim) != files or claim_source(source_path, safe_root) != claim:
                raise ValueError("Recovery source no longer matches the sealed claim")
            claims[source_path] = claim
            sealed_files.update(self._sealed_files_for_claim(claim))
        return claims, sealed_files

    def _require_records_match_claims(
        self, records: list[Any], sealed_files: dict[str, tuple[str, SourceIdentity, str]]
    ) -> None:
        """Refuse any artifact record that is not backed by an already-verified source claim."""
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Recovery artifact record is invalid")
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
                raise ValueError("Recovery artifact record is invalid")
            sealed = sealed_files.get(source_path)
            if (
                sealed is None
                or sealed[0] != safe_root
                or sealed[1] != self._decode_identity(raw_identity)
                or sealed[2] != digest
            ):
                raise ValueError("Recovery artifact does not match its verified source claim")

    @classmethod
    def _read_beneath(cls, path: str, safe_root: str) -> bytes:
        # cls dispatch bypasses instance-level monkeypatches of
        # _open_regular_beneath — a test that must intercept every open has to
        # patch the class, not the instance.
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
        readme_context: BundleReadmeContext,
        playtime_text: str,
        should_abort: Callable[[], bool] | None = None,
    ) -> str:
        """Copy, verify and atomically publish one bundle, or leave nothing behind.

        *should_abort* is polled between artifacts and between copy/hash chunks,
        so a cancelled run stops within a chunk instead of after a
        multi-hundred-megabyte copy. An abort unwinds through the same failure
        path as any other error: the staging directory is removed, and a cleanup
        that itself fails is reported rather than rewritten into a clean stop.
        """
        if _SAFE_BUNDLE_ID.fullmatch(bundle_id) is None:
            raise ValueError("unsafe recovery bundle id")
        bundles_parent = os.path.join(self._root, "bundles")
        staging_name = f".{bundle_id}.staging"
        sealed = os.path.join(bundles_parent, bundle_id)
        descriptors = self._open_layout(create=True)
        home_fd, root_fd, staging_parent_fd, bundles_parent_fd = descriptors
        staging_fd: int | None = None
        renamed = False
        preserved: str | None = None
        try:
            if (
                self._stat_at(staging_parent_fd, staging_name) is not None
                or self._stat_at(bundles_parent_fd, bundle_id) is not None
            ):
                raise FileExistsError(f"Recovery bundle already exists: {bundle_id}")
            os.mkdir(staging_name, 0o700, dir_fd=staging_parent_fd)
            staging_fd = self._open_dir_at(staging_parent_fd, staging_name)
            free_bytes = self._free_bytes_fd(root_fd)
            records, source_sets, checksums = self._copy_artifacts(staging_fd, artifacts, free_bytes, should_abort)
            enriched = dict(snapshot)
            enriched["plugin_version"] = self._plugin_version
            enriched["bundle_id"] = bundle_id
            enriched["artifacts"] = records
            enriched["source_sets"] = source_sets
            self._write_rom_states(staging_fd, enriched, checksums)
            # Rendered here, not passed in: the files/NNNNNN mapping the index
            # is built from only exists once the artifacts have been copied.
            readme = render_bundle_readme(readme_context, records)
            self._write_verified_text(staging_fd, "README.txt", readme, checksums)
            self._write_verified_text(staging_fd, "playtime.txt", playtime_text, checksums)
            self._write_verified_text(
                staging_fd,
                "manifest.json",
                json.dumps(enriched, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums,
            )
            checksum_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items()))
            checksum_digest = self._write_verified_text(staging_fd, "checksums.sha256", checksum_text, checksums=None)
            seal = {
                "bundle_id": bundle_id,
                "checksums_sha256": checksum_digest,
                "file_count": len(records),
                "sealed": True,
            }
            self._write_verified_text(
                staging_fd,
                "SEAL.json",
                json.dumps(seal, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                checksums=None,
            )
            self._verify_staging_checksums(staging_fd, checksums, should_abort)
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
                preserved = self._preserve_uncertain_bundle(bundles_parent_fd, bundles_parent, bundle_id)
                if preserved is None:
                    raise OSError("Recovery bundle durability is uncertain and no bundle remains on disk") from exc
                raise OSError(f"Recovery bundle durability is uncertain: {preserved}") from exc
            return sealed
        except BaseException as primary:
            if preserved is not None:
                raise
            self._discard_failed_bundle(bundles_parent, bundle_id, staging_name, renamed=renamed, primary=primary)
            raise
        finally:
            if staging_fd is not None:
                os.close(staging_fd)
            self._close_layout(descriptors)

    def _discard_failed_bundle(
        self,
        bundles_parent: str,
        bundle_id: str,
        staging_name: str,
        *,
        renamed: bool,
        primary: BaseException,
    ) -> None:
        """Remove what a failed seal left behind, reporting a cleanup that could not finish."""
        cleanup_path = os.path.join(
            bundles_parent if renamed else os.path.join(self._root, "staging"),
            bundle_id if renamed else staging_name,
        )
        try:
            cleanup = remove_current(cleanup_path, self._root)
            if not cleanup["success"]:
                raise RuntimeError(cleanup["message"])
        except Exception as cleanup_exc:
            raise RuntimeError(
                "Recovery bundle failed and unsafe staging was preserved at "
                f"{cleanup_path} because cleanup failed: {cleanup_exc}"
            ) from primary

    @classmethod
    def _preserve_uncertain_bundle(cls, bundles_parent_fd: int, bundles_parent: str, bundle_id: str) -> str | None:
        """Mark a sealed-but-undurable bundle and report the path that actually holds it.

        The marker rename is best-effort — the bundle is never deleted to reach a
        tidier name, so an unrenamable bundle is reported at its sealed name and a
        bundle that no longer exists is reported as ``None``. The directory is
        named from the held descriptor, so a bundle parent that was detached or
        replaced meanwhile is reported where it actually is.
        """
        uncertain_name = bundle_id + ".durability-uncertain"
        try:
            os.rename(bundle_id, uncertain_name, src_dir_fd=bundles_parent_fd, dst_dir_fd=bundles_parent_fd)
        except FileNotFoundError:
            return None
        except OSError:
            return os.path.join(cls._held_directory_path(bundles_parent_fd, bundles_parent), bundle_id)
        with contextlib.suppress(OSError):
            os.fsync(bundles_parent_fd)
        return os.path.join(cls._held_directory_path(bundles_parent_fd, bundles_parent), uncertain_name)

    @classmethod
    def _held_directory_path(cls, fd: int, lexical: str) -> str:
        """Resolve where a held directory descriptor currently lives."""
        try:
            return os.readlink(cls._fd_path(fd))
        except OSError:
            return lexical

    def _copy_artifacts(
        self,
        staging_fd: int,
        artifacts: list[RecoveryArtifact],
        free_bytes: int,
        should_abort: Callable[[], bool] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, object]], dict[str, str]]:
        expanded, source_sets, sealed_claims = self._claim_artifact_sources(artifacts, should_abort)
        self._require_free_space(expanded, free_bytes, should_abort)

        os.mkdir("files", 0o700, dir_fd=staging_fd)
        files_fd = self._open_dir_at(staging_fd, "files")
        records: list[dict[str, Any]] = []
        checksums: dict[str, str] = {}
        try:
            for index, (artifact, source) in enumerate(expanded, start=1):
                raise_if_aborted(should_abort)
                name = f"{index:06d}"
                relative = f"files/{name}"
                source_stat, source_hash, source_identity = self._copy_opened_source(
                    source, artifact["safe_root"], files_fd, name, should_abort
                )
                checksums[relative] = source_hash
                record: dict[str, Any] = {
                    "kind": artifact["kind"],
                    "source_path": source,
                    "safe_root": artifact["safe_root"],
                    "destination": relative,
                    "size": source_stat.st_size,
                    "mode": stat.S_IMODE(source_stat.st_mode),
                    "mtime_ns": source_stat.st_mtime_ns,
                    "sha256": source_hash,
                    "source_identity": source_identity,
                }
                if "rom_id" in artifact:
                    record["rom_id"] = artifact["rom_id"]
                records.append(record)
            os.fsync(files_fd)
        finally:
            os.close(files_fd)
        for source_path, sealed_claim in sealed_claims.items():
            raise_if_aborted(should_abort)
            if claim_source(source_path, sealed_claim["safe_root"], should_abort) != sealed_claim:
                raise OSError(f"Recovery source changed while the bundle was sealed: {source_path}")
        return records, source_sets, checksums

    def _claim_artifact_sources(
        self,
        artifacts: list[RecoveryArtifact],
        should_abort: Callable[[], bool] | None,
    ) -> tuple[list[tuple[RecoveryArtifact, str]], list[dict[str, object]], dict[str, SourceClaim]]:
        """Seal a claim per artifact and flatten it into the distinct files to copy."""
        expanded: list[tuple[RecoveryArtifact, str]] = []
        source_sets: list[dict[str, object]] = []
        seen: set[str] = set()
        sealed_claims: dict[str, SourceClaim] = {}
        for artifact in artifacts:
            raise_if_aborted(should_abort)
            claim = claim_source(artifact["source_path"], artifact["safe_root"], should_abort)
            files = self._files_for_claim(claim)
            sealed_claims[artifact["source_path"]] = claim
            source_sets.append(
                {
                    "source_path": artifact["source_path"],
                    "safe_root": artifact["safe_root"],
                    "files": files,
                    "kind": artifact["kind"],
                    "source_identity": claim["source_identity"],
                    "sha256": claim["sha256"],
                    "entries": claim["entries"],
                    **({"rom_id": artifact["rom_id"]} if "rom_id" in artifact else {}),
                }
            )
            for file_path in files:
                lexical = os.path.abspath(file_path)
                if lexical in seen:
                    continue
                seen.add(lexical)
                expanded.append((artifact, file_path))
        return expanded, source_sets, sealed_claims

    def _require_free_space(
        self,
        expanded: list[tuple[RecoveryArtifact, str]],
        free_bytes: int,
        should_abort: Callable[[], bool] | None,
    ) -> None:
        """Refuse the whole bundle up front unless every claimed byte fits."""
        required = 0
        for artifact, path in expanded:
            raise_if_aborted(should_abort)
            fd = self._open_regular_beneath(path, artifact["safe_root"])
            try:
                required += os.fstat(fd).st_size
            finally:
                os.close(fd)
        if free_bytes < required:
            raise OSError(f"Insufficient recovery space: need {required} bytes")

    @classmethod
    def _copy_opened_source(
        cls,
        source: str,
        safe_root: str,
        destination_parent_fd: int,
        destination_name: str,
        should_abort: Callable[[], bool] | None = None,
    ) -> tuple[os.stat_result, str, SourceIdentity]:
        # cls dispatch bypasses instance-level monkeypatches of
        # _open_regular_beneath — a test that must intercept every open has to
        # patch the class, not the instance.
        source_fd = cls._open_regular_beneath(source, safe_root)
        try:
            before = os.fstat(source_fd)
            source_identity = identity_for_stat(before, mount_id_for_fd(source_fd))
            digest = hashlib.sha256()
            destination_fd = os.open(
                destination_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=destination_parent_fd,
            )
            try:
                while block := os.read(source_fd, 1024 * 1024):
                    raise_if_aborted(should_abort)
                    digest.update(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_fd, view)
                        view = view[written:]
                os.fchmod(destination_fd, stat.S_IMODE(before.st_mode))
                os.utime(destination_fd, ns=(before.st_atime_ns, before.st_mtime_ns))
                destination_hash = cls._sha256_fd(destination_fd, should_abort)
                if digest.hexdigest() != destination_hash:
                    raise OSError(f"Recovery checksum mismatch for {source}")
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            after = os.fstat(source_fd)
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if identity_after != identity_before:
                raise OSError(f"Recovery source changed while it was copied: {source}")
            return before, digest.hexdigest(), source_identity
        finally:
            os.close(source_fd)

    @staticmethod
    def _files_for_claim(claim: SourceClaim) -> list[str]:
        if not claim["source_identity"]["exists"]:
            return []
        if claim["sha256"] is not None:
            return [claim["source_path"]]
        return sorted(
            os.path.join(claim["source_path"], *relative.split("/"))
            for relative, entry in claim["entries"].items()
            if "sha256" in entry
        )

    @staticmethod
    def _sealed_files_for_claim(claim: SourceClaim) -> dict[str, tuple[str, SourceIdentity, str]]:
        """Index a verified claim's regular-file members by absolute path."""
        if not claim["source_identity"]["exists"]:
            return {}
        if claim["sha256"] is not None:
            return {claim["source_path"]: (claim["safe_root"], claim["source_identity"], claim["sha256"])}
        indexed: dict[str, tuple[str, SourceIdentity, str]] = {}
        for relative, entry in claim["entries"].items():
            digest = entry.get("sha256")
            if digest is None:
                continue
            absolute = os.path.join(claim["source_path"], *relative.split("/"))
            indexed[absolute] = (claim["safe_root"], entry["identity"], digest)
        return indexed

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
        root_mount_id = mount_id_for_fd(directory_fd)
        try:
            for component in parts[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
                    dir_fd=directory_fd,
                )
                if mount_id_for_fd(next_fd) != root_mount_id:
                    os.close(next_fd)
                    raise ValueError(f"Recovery source crosses a mount boundary: {path}")
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=directory_fd)
            if mount_id_for_fd(file_fd) != root_mount_id:
                os.close(file_fd)
                raise ValueError(f"Recovery source crosses a mount boundary: {path}")
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                os.close(file_fd)
                raise ValueError(f"Recovery source is not a regular file: {path}")
            return file_fd
        finally:
            os.close(directory_fd)

    def _write_rom_states(self, staging_fd: int, snapshot: dict[str, object], checksums: dict[str, str]) -> None:
        rows = snapshot.get("roms")
        if not isinstance(rows, list):
            return
        os.mkdir("roms", 0o700, dir_fd=staging_fd)
        roms_fd = self._open_dir_at(staging_fd, "roms")
        try:
            for row in rows:
                if not isinstance(row, dict) or type(row.get("rom_id")) is not int or row["rom_id"] <= 0:
                    raise ValueError("Recovery snapshot contains an invalid ROM id")
                rom_id = int(row["rom_id"])
                os.mkdir(str(rom_id), 0o700, dir_fd=roms_fd)
                directory_fd = self._open_dir_at(roms_fd, str(rom_id))
                try:
                    per_rom = {
                        key: [item for item in value if isinstance(item, dict) and item.get("rom_id") == rom_id]
                        if isinstance(value, list)
                        else value
                        for key, value in snapshot.items()
                        if key in {"roms", "installs", "metadata", "save_sync", "playtime"}
                    }
                    relative = f"roms/{rom_id}/state.json"
                    digest = self._write_file_at(
                        directory_fd,
                        "state.json",
                        (json.dumps(per_rom, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                    )
                    checksums[relative] = digest
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            os.fsync(roms_fd)
        finally:
            os.close(roms_fd)

    def _write_verified_text(
        self, staging_fd: int, relative: str, content: str, checksums: dict[str, str] | None
    ) -> str:
        parent_fd, name = self._open_relative_parent(staging_fd, relative)
        try:
            digest = self._write_file_at(parent_fd, name, content.encode("utf-8"))
        finally:
            os.close(parent_fd)
        if checksums is not None:
            checksums[relative] = digest
        return digest

    @classmethod
    def _write_file_at(cls, parent_fd: int, name: str, content: bytes) -> str:
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            digest = cls._sha256_fd(fd)
            os.fsync(fd)
            return digest
        finally:
            os.close(fd)

    @classmethod
    def _open_relative_parent(cls, root_fd: int, relative: str) -> tuple[int, str]:
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Recovery destination path is invalid")
        fd = os.dup(root_fd)
        root_mount_id = mount_id_for_fd(root_fd)
        try:
            for component in parts[:-1]:
                next_fd = cls._open_dir_at(fd, component)
                if mount_id_for_fd(next_fd) != root_mount_id:
                    os.close(next_fd)
                    raise ValueError("Recovery destination crosses a mount boundary")
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def _verify_staging_checksums(
        cls, staging_fd: int, checksums: dict[str, str], should_abort: Callable[[], bool] | None = None
    ) -> None:
        for relative, expected in checksums.items():
            raise_if_aborted(should_abort)
            parent_fd, name = cls._open_relative_parent(staging_fd, relative)
            try:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode) or cls._sha256_fd(fd, should_abort) != expected:
                        raise OSError(f"Recovery staging checksum mismatch: {relative}")
                finally:
                    os.close(fd)
            finally:
                os.close(parent_fd)

    @staticmethod
    def _sha256_fd(fd: int, should_abort: Callable[[], bool] | None = None) -> str:
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        while block := os.read(fd, 1024 * 1024):
            raise_if_aborted(should_abort)
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
            if create:
                self._write_root_readme(root_fd)
            return home_fd, root_fd, staging_fd, bundles_fd
        except BaseException:
            for fd in (bundles_fd, staging_fd, root_fd, home_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise

    @staticmethod
    def _write_root_readme(root_fd: int) -> None:
        """Explain the recovery root itself, once, when it is first created.

        Written here rather than on preview: reading free space must never
        bring this directory into existence, so the only moment the root is
        known to be wanted is the one that creates it. Best-effort — a bundle
        must never fail to seal because its folder's signpost could not be
        written.
        """
        with contextlib.suppress(OSError):
            if RecoveryBundleAdapter._stat_at(root_fd, _ROOT_README_NAME) is not None:
                return
            fd = os.open(_ROOT_README_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            try:
                os.write(fd, _ROOT_README_TEXT.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _close_layout(descriptors: tuple[int, int, int, int]) -> None:
        for fd in reversed(descriptors):
            os.close(fd)

    @staticmethod
    def _open_or_create_dir(parent_fd: int, name: str, *, create: bool) -> int:
        try:
            result = RecoveryBundleAdapter._open_dir_at(parent_fd, name)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            result = RecoveryBundleAdapter._open_dir_at(parent_fd, name)
        except OSError as exc:
            raise ValueError(f"Recovery directory is not a trusted directory: {name}") from exc
        if mount_id_for_fd(result) != mount_id_for_fd(parent_fd):
            os.close(result)
            raise ValueError(f"Recovery directory crosses a mount boundary: {name}")
        return result

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
            bundle_fd = self._open_dir_at(descriptors[3], bundle_id)
            if mount_id_for_fd(bundle_fd) != mount_id_for_fd(descriptors[3]):
                os.close(bundle_fd)
                raise ValueError("Recovery bundle crosses a mount boundary")
            return descriptors, bundle_fd
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
            "mount_id": integer("mount_id"),
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
        raw_sha256: str | None,
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
            "sha256": raw_sha256,
            "entries": entries,
            "content_bound": True,
        }
