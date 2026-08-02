"""Save-side collaborator serving removed-game cleanup over the saves aggregate.

Everything the prune bounded context needs from the saves context and nothing
else: which local save paths a purge set owns, the locks that hold them still,
the sanctioned quarantine of the ones it exclusively owns, and the absence proof
finalization requires before it cascades.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.save_backup import BACKUP_DIR_NAME, backup_name, is_backup_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from models.prune import MutationOutcome, SourceClaim

    from domain.rom_install import RomInstall
    from services.protocols import Clock, RetroDeckPaths, SaveFileStore, UnitOfWorkFactory
    from services.saves.rom_info import RomInfoService
    from services.saves.sync_engine import SyncEngine


def _save_identity(platform_slug: str, content_path: str) -> tuple[str, str]:
    """The identity two local ROM rows share when they project onto the same save files.

    A ROM's save files are ``<saves_root>/<platform's content dir>/<content
    stem><ext>``: the directory follows from the platform (a single-file ROM
    installs into that platform's system directory) and the stem is the content
    filename without its extension, exactly as ``RomInfoService`` derives
    ``rom_name`` from an install's ``file_path``. Two rows agreeing on both
    therefore land on the same paths, whether or not either is installed —
    which is what lets an *uninstalled* row be recognised as a co-owner of an
    installed row's saves.

    ``content_path`` is an install's full ``file_path`` or a row's bare
    ``fs_name``; only its basename stem is read, so the two are interchangeable.
    """
    return (platform_slug, os.path.splitext(os.path.basename(content_path))[0])


def _is_plain_basename(filename: str) -> bool:
    """Whether a tracked filename is a bare name safe to project onto a saves dir."""
    return filename not in {"", ".", ".."} and os.path.basename(filename) == filename and "\x00" not in filename


def _current_save_artifact(path: str, saves_root: str, rom_id: int) -> dict[str, object]:
    return {"source_path": path, "safe_root": saves_root, "kind": "current_save", "rom_id": rom_id}


@dataclass(frozen=True)
class PruneSaveSupportConfig:
    """Construction-time wiring for the removed-game-cleanup save collaborator."""

    uow_factory: UnitOfWorkFactory
    save_file_store: SaveFileStore
    retrodeck_paths: RetroDeckPaths
    clock: Clock
    rom_info: RomInfoService
    sync_engine: SyncEngine


class PruneSaveSupport:
    """Satisfy the ``PruneSaveCoordinator`` seam against the saves aggregate."""

    def __init__(self, *, config: PruneSaveSupportConfig) -> None:
        self._uow_factory = config.uow_factory
        self._save_file_store = config.save_file_store
        self._retrodeck_paths = config.retrodeck_paths
        self._clock = config.clock
        self._rom_info = config.rom_info
        self._sync_engine = config.sync_engine

    @contextlib.asynccontextmanager
    async def lock_prune_roms(self, rom_ids: list[int]) -> AsyncIterator[None]:
        """Hold affected save locks in ascending id order for recovery/removal."""
        async with contextlib.AsyncExitStack() as stack:
            for rom_id in sorted({int(value) for value in rom_ids}):
                await stack.enter_async_context(self._sync_engine.rom_lock(rom_id))
            yield

    def inventory_prune_saves(self, purge_rom_ids: list[int]) -> dict[str, Any]:
        """Build exact-path save ownership and recovery artifacts for a purge set.

        Ownership spans every local ``roms`` row that lands on a path, not only
        the installed ones: a vanished row's live replacement is routinely
        uninstalled, and treating its shared save as exclusive would quarantine
        a file the replacement still reads.
        """
        purge_ids = {int(value) for value in purge_rom_ids}
        installs, persisted_names, uninstalled_by_identity = self._local_save_state()
        ownership, expected_by_id = self._project_ownership(installs, persisted_names, uninstalled_by_identity)
        return self._inventory_for(purge_ids, ownership, expected_by_id)

    def _local_save_state(self) -> tuple[list[RomInstall], dict[int, list[str]], dict[tuple[str, str], set[int]]]:
        """Read, in one short UoW, everything path projection needs from SQLite."""
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
            installed_ids = {install.rom_id for install in installs}
            persisted_names = {
                rom_id: list(state.files)
                for rom_id, state in uow.rom_save_sync_states.iter_all()
                if rom_id in installed_ids
            }
            # An uninstalled row has no install record to resolve a path from, so
            # it is matched onto an installed row's paths by save identity.
            uninstalled_by_identity: dict[tuple[str, str], set[int]] = {}
            for rom in uow.roms.iter_all():
                if rom.rom_id in installed_ids:
                    continue
                identity = _save_identity(rom.platform_slug, rom.fs_name)
                uninstalled_by_identity.setdefault(identity, set()).add(rom.rom_id)
        return installs, persisted_names, uninstalled_by_identity

    def _project_ownership(
        self,
        installs: list[RomInstall],
        persisted_names: dict[int, list[str]],
        uninstalled_by_identity: dict[tuple[str, str], set[int]],
    ) -> tuple[dict[str, set[int]], dict[int, list[dict[str, str]]]]:
        """Map every projected save path to the full set of rows that land on it."""
        ownership: dict[str, set[int]] = {}
        expected_by_id: dict[int, list[dict[str, str]]] = {}
        for install in installs:
            rom_id = install.rom_id
            expected = self._expected_save_files(rom_id, persisted_names.get(rom_id, []))
            expected_by_id[rom_id] = expected
            owners = {rom_id} | uninstalled_by_identity.get(
                _save_identity(install.platform_slug, install.file_path), set()
            )
            for item in expected:
                ownership.setdefault(self._save_file_store.canonical_path(item["path"]), set()).update(owners)
        return ownership, expected_by_id

    def _expected_save_files(self, rom_id: int, persisted: list[str]) -> list[dict[str, str]]:
        """The ROM's projected save files, plus any tracked name discovery missed.

        A filename the aggregate recorded but discovery no longer projects (the
        ROM was renamed on the server) still names a real file on disk, so it is
        carried — but only when it is a plain basename, since a tracked value is
        server-derived and must never widen the path set.
        """
        expected = self._rom_info.expected_save_files(rom_id)
        if not expected:
            return expected
        saves_dir = expected[0]["saves_dir"]
        known = {item["filename"] for item in expected}
        for filename in persisted:
            if filename in known or not _is_plain_basename(filename):
                continue
            expected.append({"path": os.path.join(saves_dir, filename), "filename": filename, "saves_dir": saves_dir})
        return expected

    def _inventory_for(
        self,
        purge_ids: set[int],
        ownership: dict[str, set[int]],
        expected_by_id: dict[int, list[dict[str, str]]],
    ) -> dict[str, Any]:
        """Classify every purge-set save path into the recovery/quarantine buckets."""
        saves_root = self._retrodeck_paths.saves_path()
        artifacts: list[dict[str, object]] = []
        exclusive: list[dict[str, str]] = []
        shared: list[str] = []
        warnings: list[str] = []
        source_claims: dict[str, SourceClaim] = {}
        lock_ids = set(purge_ids)
        for rom_id in sorted(purge_ids):
            expected = expected_by_id.get(rom_id, [])
            if not expected:
                warnings.append(f"ROM {rom_id}: save path could not be resolved; physical saves were left untouched")
                continue
            for item in expected:
                path = item["path"]
                if not self._save_file_store.is_within(path, saves_root):
                    warnings.append(f"ROM {rom_id}: save path is outside the supported saves root; left untouched")
                    continue
                owners = ownership.get(self._save_file_store.canonical_path(path), {rom_id})
                lock_ids.update(owners)
                if owners <= purge_ids:
                    artifacts.append(_current_save_artifact(path, saves_root, rom_id))
                    exclusive.append(item)
                    source_claims[path] = self._save_file_store.claim_source(path, saves_root)
                elif self._save_file_store.is_file(path):
                    artifacts.append(_current_save_artifact(path, saves_root, rom_id))
                    shared.append(path)
                artifacts.extend(self._backup_artifacts(item, rom_id, saves_root))
        return {
            "artifacts": artifacts,
            "exclusive": exclusive,
            "shared": sorted(set(shared)),
            "warnings": warnings,
            "lock_rom_ids": sorted(lock_ids),
            "source_claims": source_claims,
        }

    def _backup_artifacts(self, item: dict[str, str], rom_id: int, saves_root: str) -> list[dict[str, object]]:
        """Every ``.romm-backup`` history file belonging to one projected save."""
        backup_dir = os.path.join(item["saves_dir"], BACKUP_DIR_NAME)
        if self._save_file_store.is_symlink(backup_dir) or not self._save_file_store.is_within(backup_dir, saves_root):
            raise ValueError(f"ROM {rom_id}: save backup directory is unsafe: {backup_dir}")
        found: list[dict[str, object]] = []
        for entry in self._save_file_store.listdir(backup_dir):
            backup_path = os.path.join(backup_dir, entry)
            if is_backup_for(item["filename"], entry) and self._save_file_store.is_file(backup_path):
                found.append(
                    {"source_path": backup_path, "safe_root": saves_root, "kind": "save_backup", "rom_id": rom_id}
                )
        return found

    def quarantine_prune_saves(
        self, files: list[dict[str, str]], claims: dict[str, SourceClaim] | None = None
    ) -> dict[str, Any]:
        """Move exclusive current saves through the sanctioned backup funnel."""
        moved: list[str] = []
        saves_root = self._retrodeck_paths.saves_path()
        try:
            for item in files:
                backup_dir = os.path.join(item["saves_dir"], BACKUP_DIR_NAME)
                if (
                    not self._save_file_store.is_within(item["path"], saves_root)
                    or not self._save_file_store.is_within(backup_dir, saves_root)
                    or self._save_file_store.is_symlink(backup_dir)
                ):
                    raise ValueError(f"Unsafe save quarantine destination: {backup_dir}")
                claim = claims.get(item["path"]) if claims is not None else None
                if claim is None:
                    claim = self._save_file_store.claim_source(item["path"], saves_root)
                outcome = self._quarantine_claimed_file(
                    item["saves_dir"], item["filename"], claim=claim, safe_root=saves_root
                )
                if outcome["changed"]:
                    moved += [item["path"]]
                if not outcome["success"]:
                    return {
                        "success": False,
                        "reason": "save_quarantine_failed",
                        "message": outcome["message"],
                        "moved": moved,
                        "ambiguous": outcome["ambiguous"],
                    }
        except Exception as exc:
            return {
                "success": False,
                "reason": "save_quarantine_failed",
                "message": str(exc),
                "moved": moved,
                "ambiguous": False,
            }
        return {"success": True, "moved": moved, "ambiguous": False}

    def validate_prune_absences(self, claims: dict[str, SourceClaim]) -> bool:
        """Require every quarantined purge-owned path to remain absent before cascade."""
        saves_root = self._retrodeck_paths.saves_path()
        try:
            for path in claims:
                current = self._save_file_store.claim_source(path, saves_root)
                if current["source_identity"]["exists"]:
                    return False
        except Exception:
            return False
        return True

    def _quarantine_claimed_file(
        self,
        saves_dir: str,
        filename: str,
        *,
        claim: SourceClaim,
        safe_root: str,
    ) -> MutationOutcome:
        """Durably quarantine one exact claimed save through anchored directories."""
        local_path = os.path.join(saves_dir, filename)
        backup_dir = os.path.join(saves_dir, BACKUP_DIR_NAME)
        self._save_file_store.ensure_directory(backup_dir, safe_root)
        ts = self._clock.now().strftime("%Y%m%d_%H%M%S")
        backup = backup_name(filename, ts, set(self._save_file_store.listdir(backup_dir)))
        return self._save_file_store.rename_claimed(
            local_path,
            os.path.join(backup_dir, backup),
            safe_root,
            claim,
        )


__all__ = ["PruneSaveSupport", "PruneSaveSupportConfig"]
