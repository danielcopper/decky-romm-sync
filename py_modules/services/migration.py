"""MigrationService — RetroDECK path and save-sort migration orchestration.

Owns the runtime decisions for relocating ROMs, BIOS, and save files
when the RetroDECK home path changes or RetroArch save sorting flips.
All raw filesystem I/O is delegated to the ``MigrationFileStore``
Protocol; conflict resolution, state mutations, and event emission
remain the service's responsibility.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.save_extensions import get_save_extensions
from domain.save_path import resolve_save_dir

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from models.state import SaveSortSettings

    from domain.rom_install import RomInstall
    from services.protocols import (
        CoreNameProviderFn,
        CoreResolverFn,
        EventEmitter,
        MigrationFileStore,
        RetroArchSaveSortingProvider,
        RetroDeckPaths,
        SettingsPersister,
        UnitOfWorkFactory,
    )

# kv_config keys for the cross-run change-detection markers MigrationService
# diffs (ADR-0003 Bucket 2): the last-seen RetroDECK home and RetroArch
# save-sort observation, each with a ``_previous`` companion that exists only
# while a migration is awaiting user confirmation.
_KV_RETRODECK_HOME = "retrodeck_home_path"
_KV_RETRODECK_HOME_PREVIOUS = "retrodeck_home_path_previous"
_KV_SAVE_SORT = "save_sort_settings"
_KV_SAVE_SORT_PREVIOUS = "save_sort_settings_previous"


@dataclass(frozen=True)
class MigrationServiceConfig:
    """Frozen wiring bundle handed to ``MigrationService.__init__``.

    Holds the Protocol-typed migration-file adapter, the live settings
    dict, runtime infrastructure, persistence callbacks, event emitter,
    and the provider callables MigrationService needs at construction
    time. Relational migration state (ROM installs, BIOS records, change
    markers) is read through the injected ``uow_factory``.
    """

    migration_file_store: MigrationFileStore
    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    settings_persister: SettingsPersister
    emit: EventEmitter
    get_bios_files_index: Callable[[], dict[str, dict[str, Any]]]
    retrodeck_paths: RetroDeckPaths
    get_retroarch_save_sorting: RetroArchSaveSortingProvider
    get_active_core: CoreResolverFn
    get_core_name: CoreNameProviderFn
    uow_factory: UnitOfWorkFactory


class MigrationService:
    """Handles RetroDECK path change detection and file migration."""

    def __init__(self, *, config: MigrationServiceConfig) -> None:
        self._migration_file_store = config.migration_file_store
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._settings_persister = config.settings_persister
        self._emit = config.emit
        self._get_bios_files_index = config.get_bios_files_index
        self._retrodeck_paths = config.retrodeck_paths
        self._get_retroarch_save_sorting = config.get_retroarch_save_sorting
        self._get_active_core = config.get_active_core
        self._get_core_name = config.get_core_name
        self._uow_factory = config.uow_factory
        # Strong refs to in-flight background tasks. ``loop.create_task``
        # alone is not enough — without a strong ref, the loop is free to
        # garbage-collect the task before it completes. ``add_done_callback``
        # prunes finished entries to keep the set bounded.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background_task(self, coro) -> asyncio.Task[Any]:
        """Schedule ``coro`` on the plugin loop and track the task for shutdown.

        Wraps ``loop.create_task`` so the resulting task is retained in
        ``_background_tasks`` until completion. ``shutdown()`` cancels any
        still-pending entries on plugin unload.
        """
        task = self._loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Cancel any in-flight background tasks and await their completion.

        Called from ``main._unload`` so RetroDECK path-change notification
        coroutines do not leak across the plugin unload boundary. No-op
        when no tasks are pending.
        """
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def detect_retrodeck_path_change(self) -> None:
        """Check if RetroDECK home path changed since last run."""
        current_home = self._retrodeck_paths.retrodeck_home()
        with self._uow_factory() as uow:
            stored_home = uow.kv_config.get(_KV_RETRODECK_HOME) or ""
            stored_previous = uow.kv_config.get(_KV_RETRODECK_HOME_PREVIOUS) or ""

        if not current_home:
            return

        if not self._migration_file_store.is_dir(current_home):
            self._logger.warning(f"RetroDECK home path does not exist, skipping: {current_home}")
            return

        if stored_home == current_home:
            return

        if not stored_home:
            # First run — just store the current path, no migration needed
            with self._uow_factory() as uow:
                uow.kv_config.set(_KV_RETRODECK_HOME, current_home)
            return

        # Auto-clear: user reverted RetroDECK to the previous home before migrating.
        # The "previous" path is now the live one — no migration needed, drop the marker.
        if current_home == stored_previous:
            previous = stored_previous
            with self._uow_factory() as uow:
                uow.kv_config.delete(_KV_RETRODECK_HOME_PREVIOUS)
                uow.kv_config.set(_KV_RETRODECK_HOME, current_home)
            self._logger.info(f"RetroDECK home reverted to previous path; clearing migration marker: {current_home}")
            # Notify the frontend so any pending migration UI can dismiss itself.
            # ``cleared: True`` lets the listener distinguish from the path-change emit.
            self._spawn_background_task(
                self._emit(
                    "retrodeck_path_changed",
                    {
                        "old_path": previous,
                        "new_path": current_home,
                        "cleared": True,
                    },
                )
            )
            return

        old_home = stored_home

        # Path changed — store both old and new, emit event
        with self._uow_factory() as uow:
            uow.kv_config.set(_KV_RETRODECK_HOME_PREVIOUS, old_home)
            uow.kv_config.set(_KV_RETRODECK_HOME, current_home)
        self._logger.warning(f"RetroDECK home path changed: {old_home} -> {current_home}")
        self._spawn_background_task(
            self._emit(
                "retrodeck_path_changed",
                {
                    "old_path": old_home,
                    "new_path": current_home,
                },
            )
        )

    def is_retrodeck_migration_pending(self) -> bool:
        """Return True if a RetroDECK home path migration is pending."""
        with self._uow_factory() as uow:
            return bool(uow.kv_config.get(_KV_RETRODECK_HOME_PREVIOUS))

    def dismiss_retrodeck_migration(self) -> dict[str, Any]:
        """Dismiss the RetroDECK path migration warning without migrating files."""
        with self._uow_factory() as uow:
            uow.kv_config.delete(_KV_RETRODECK_HOME_PREVIOUS)
        return {"success": True}

    def _collect_rom_items(self, old_home, new_home, installs, relocations):
        """Collect ROM migration items from the installed-ROM records.

        ``installs`` is a pre-snapshotted list of ``RomInstall`` records (the
        caller opens the read UoW). Emits exactly **one** move unit per install
        (never both a file and its enclosing directory): a folder-backed ROM
        (``rom_dir`` set) moves the whole directory as a unit — kind
        ``"rom_dir"`` — so sibling disc/update/DLC files travel with it; a
        single-file ROM (``rom_dir`` is ``None``) moves just the launch file —
        kind ``"rom"``. Each item carries a success-hook updater that records
        the intended relocation into *relocations* (keyed by ``rom_id``) when
        its move/skip succeeds; the relocations are applied to ``rom_installs``
        in a separate write UoW after the file moves complete (ADR-0006).
        """
        items = []
        for install in installs:
            rom_dir = install.rom_dir
            if rom_dir and rom_dir.startswith(old_home + os.sep):
                # Multi-file ROM: move the whole dedicated directory as a unit.
                # The launch file lives inside it, so its new path follows the
                # directory's new location.
                new_rom_dir = os.path.join(new_home, os.path.relpath(rom_dir, old_home))
                new_file_path = os.path.join(new_rom_dir, os.path.relpath(install.file_path, rom_dir))
                items.append(
                    (
                        os.path.basename(rom_dir),
                        rom_dir,
                        new_rom_dir,
                        self._make_rom_relocation_updater(relocations, install.rom_id, new_rom_dir, new_file_path),
                        "rom_dir",
                    )
                )
            else:
                # Single-file ROM: move only the launch file; it owns no folder.
                file_path = install.file_path
                if not file_path or not file_path.startswith(old_home + os.sep):
                    continue
                new_file_path = os.path.join(new_home, os.path.relpath(file_path, old_home))
                items.append(
                    (
                        os.path.basename(file_path),
                        file_path,
                        new_file_path,
                        self._make_rom_relocation_updater(relocations, install.rom_id, None, new_file_path),
                        "rom",
                    )
                )
        return items

    @staticmethod
    def _make_rom_relocation_updater(relocations, rom_id, new_rom_dir, new_file_path):
        """Build a success-hook closure that records one install's new paths.

        The migration loop calls the returned updater once the move (or skip)
        for this install's single move unit succeeds. It records the new
        ``rom_dir`` (``None`` for a single-file ROM) and ``file_path`` per
        ``rom_id`` into *relocations* so the post-move write UoW can apply
        ``RomInstall.relocate`` for each moved install.
        """

        def update():
            relocations[rom_id] = {"rom_dir": new_rom_dir, "file_path": new_file_path}

        return update

    def _collect_tracked_bios_items(self, old_home, new_home, bios_files, relocations):
        """Collect tracked BIOS migration items from the ``BiosFile`` snapshot.

        ``bios_files`` is a pre-snapshotted list of ``BiosFile`` records (the
        caller opens the read UoW). Each item carries a success-hook updater
        that records the intended new ``file_path`` into *relocations* (keyed by
        the aggregate's composite identity ``(platform_slug, file_name)``) when
        its move/skip succeeds; the relocations are applied to ``bios_files`` in
        a separate write UoW after the file moves complete (ADR-0006).
        """
        items = []
        for bios_file in bios_files:
            file_path = bios_file.file_path
            if not file_path or not file_path.startswith(old_home + os.sep):
                continue
            new_path = os.path.join(new_home, os.path.relpath(file_path, old_home))
            key = (bios_file.platform_slug, bios_file.file_name)
            items.append(
                (
                    bios_file.file_name,
                    file_path,
                    new_path,
                    self._make_bios_relocation_updater(relocations, key, new_path),
                    "bios",
                )
            )
        return items

    @staticmethod
    def _make_bios_relocation_updater(relocations, key, new_path):
        """Build a success-hook closure that records one BIOS file's new path.

        The migration loop calls the returned updater once the move (or skip)
        for this BIOS file succeeds. It records the new ``file_path`` keyed by
        the aggregate's composite identity ``(platform_slug, file_name)`` into
        *relocations* so the post-move write UoW can apply ``BiosFile.relocate``
        for each moved record.
        """

        def update():
            relocations[key] = new_path

        return update

    def _collect_untracked_bios_items(self, old_home, tracked_file_names):
        """Collect untracked BIOS migration items (downloaded before state tracking).

        ``tracked_file_names`` is the set of BIOS file names already covered by
        the ``BiosFile`` snapshot, so those tracked records aren't moved twice.
        Untracked BIOS files have no aggregate record, so their move carries a
        no-op updater (nothing to persist).
        """
        items = []
        old_bios = os.path.join(old_home, "bios")
        new_bios = self._retrodeck_paths.bios_path()
        if not self._migration_file_store.is_dir(old_bios):
            return items
        for file_name, reg_entry in self._get_bios_files_index().items():
            if file_name in tracked_file_names:
                continue
            firmware_path = reg_entry.get("firmware_path", file_name)
            old_file = os.path.join(old_bios, firmware_path)
            new_file = os.path.join(new_bios, firmware_path)
            if not self._migration_file_store.exists(old_file):
                continue
            items.append((file_name, old_file, new_file, lambda: None, "bios"))
        return items

    def _collect_save_items(self, old_home):
        """Collect save file migration items by scanning old saves directory.

        Hidden directories (those whose name begins with ``.``) and the
        files they contain are skipped: the RomM plugin's ``.romm-backup``
        sidecars and any ad-hoc user dotdirs must not be migrated.
        """
        items = []
        old_saves = os.path.join(old_home, "saves")
        new_saves = self._retrodeck_paths.saves_path()
        if not self._migration_file_store.is_dir(old_saves):
            return items
        for dirpath, _dirs, filenames in self._migration_file_store.walk_files(old_saves):
            rel_dir = os.path.relpath(dirpath, old_saves)
            # Skip any descendant of a hidden directory by inspecting the
            # relative-path segments. ``rel_dir == "."`` for the saves
            # root itself, which is never hidden.
            if rel_dir != "." and any(part.startswith(".") for part in rel_dir.split(os.sep)):
                continue
            for fname in filenames:
                if fname.startswith("."):
                    continue
                old_file = os.path.join(dirpath, fname)
                rel = os.path.relpath(old_file, old_saves)
                new_file = os.path.join(new_saves, rel)
                items.append((rel, old_file, new_file, lambda: None, "save"))
        return items

    def _collect_migration_items(self, old_home, new_home, installs, bios_files, relocations, bios_relocations):
        """Collect all files that need migration across ROMs, BIOS, and saves.

        Returns list of (label, old_path, new_path, state_update_fn, kind) tuples.
        state_update_fn is called after a successful move/skip to update state.
        ``installs``/``bios_files`` are the pre-snapshotted ``RomInstall`` and
        ``BiosFile`` lists; ``relocations`` accumulates the per-``rom_id`` new
        paths and ``bios_relocations`` the per-``(platform_slug, file_name)`` new
        paths for the post-move write UoW.
        """
        tracked_bios_names = {bf.file_name for bf in bios_files}
        items = []
        items.extend(self._collect_rom_items(old_home, new_home, installs, relocations))
        items.extend(self._collect_tracked_bios_items(old_home, new_home, bios_files, bios_relocations))
        items.extend(self._collect_untracked_bios_items(old_home, tracked_bios_names))
        items.extend(self._collect_save_items(old_home))
        return items

    def _find_conflicts(self, items):
        """Return sorted list of labels where both source and destination exist."""
        conflict_set = set()
        for label, old_path, new_path, _updater, _kind in items:
            if self._migration_file_store.exists(new_path) and self._migration_file_store.exists(old_path):
                conflict_set.add(label)
        return sorted(conflict_set)

    def _migrate_single_item(self, label, old_path, new_path, state_updater, kind, conflict_strategy, counts, errors):
        """Migrate a single file/directory item. Updates counts and errors in place."""
        # A moved per-ROM directory ("rom_dir") counts as one migrated ROM, same
        # as a single-file ROM ("rom") — both fold into the "rom" counter.
        count_key = "rom" if kind in ("rom", "rom_dir") else kind

        if not self._migration_file_store.exists(old_path):
            if self._migration_file_store.exists(new_path):
                state_updater()
                if count_key:
                    counts[count_key] = counts.get(count_key, 0) + 1
            return

        if self._migration_file_store.exists(new_path):
            self._migrate_conflict_item(
                label,
                old_path,
                new_path,
                state_updater,
                conflict_strategy,
                count_key,
                counts,
                errors,
            )
            return

        try:
            self._migration_file_store.make_dirs(os.path.dirname(new_path))
            self._migration_file_store.move(old_path, new_path)
            state_updater()
            if count_key:
                counts[count_key] = counts.get(count_key, 0) + 1
            self._logger.info(f"Migrated {kind}: {old_path} -> {new_path}")
        except OSError as e:
            errors.append(f"{label}: {e}")
            self._logger.error(f"Migration failed: {old_path}: {e}")

    def _migrate_conflict_item(
        self,
        label,
        old_path,
        new_path,
        state_updater,
        conflict_strategy,
        count_key,
        counts,
        errors,
    ):
        """Handle migration when destination already exists."""
        if conflict_strategy == "overwrite":
            try:
                if self._migration_file_store.is_dir(new_path):
                    self._migration_file_store.remove_tree(new_path)
                else:
                    self._migration_file_store.remove_file(new_path)
                self._migration_file_store.make_dirs(os.path.dirname(new_path))
                self._migration_file_store.move(old_path, new_path)
                state_updater()
                if count_key:
                    counts[count_key] = counts.get(count_key, 0) + 1
                self._logger.info(f"Migration overwrite: {old_path} -> {new_path}")
            except OSError as e:
                errors.append(f"{label}: {e}")
                self._logger.error(f"Migration overwrite failed: {old_path}: {e}")
        else:
            # skip — keep destination, update state
            state_updater()
            if count_key:
                counts[count_key] = counts.get(count_key, 0) + 1
            self._logger.info(f"Migration skip (exists): {new_path}")

    @staticmethod
    def _build_migration_result(counts, errors):
        """Build the result dict from migration counts and errors."""
        parts = []
        if counts["rom"]:
            parts.append(f"{counts['rom']} ROM(s)")
        if counts["bios"]:
            parts.append(f"{counts['bios']} BIOS")
        if counts["save"]:
            parts.append(f"{counts['save']} save(s)")
        msg = f"Migrated {', '.join(parts)}" if parts else "No files to migrate"
        if errors:
            msg += f" ({len(errors)} error(s))"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "roms_moved": counts["rom"],
            "bios_moved": counts["bios"],
            "saves_moved": counts["save"],
            "errors": errors,
        }

    def _migrate_retrodeck_files_io(self, old_home, new_home, conflict_strategy):
        """Sync helper for migrate_retrodeck_files — FS traversal + moves in executor."""
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
            bios_files = list(uow.bios_files.iter_all())
        relocations: dict[int, dict[str, str]] = {}
        bios_relocations: dict[tuple[str, str], str] = {}
        items = self._collect_migration_items(old_home, new_home, installs, bios_files, relocations, bios_relocations)
        conflicts = self._find_conflicts(items)

        # If no strategy given and there are conflicts, return them for user decision
        if conflict_strategy is None and conflicts:
            return {
                "success": False,
                "needs_confirmation": True,
                "conflict_count": len(conflicts),
                "conflicts": conflicts,
                "message": f"{len(conflicts)} file(s) already exist at destination",
            }

        counts = {"rom": 0, "bios": 0, "save": 0}
        errors = []

        for label, old_path, new_path, state_updater, kind in items:
            self._migrate_single_item(
                label,
                old_path,
                new_path,
                state_updater,
                kind,
                conflict_strategy,
                counts,
                errors,
            )

        # Apply the relocations the per-item updaters accumulated and clear the
        # previous-path marker, all in one short write UoW after the file moves.
        self._apply_relocations(installs, relocations, bios_files, bios_relocations, clear_marker=not errors)

        return self._build_migration_result(counts, errors)

    def _apply_relocations(self, installs, relocations, bios_files, bios_relocations, *, clear_marker):
        """Persist the relocated install and BIOS records (and optionally clear the marker).

        Opens a single write UoW after the file moves: for every install whose
        move/skip succeeded, calls ``RomInstall.relocate`` with the new
        ``rom_dir`` (``None`` for a single-file ROM) and ``file_path``; for every
        BIOS record whose move/skip succeeded, calls ``BiosFile.relocate`` with
        the new ``file_path``. Both are saved in the same transaction. When
        *clear_marker* is true (a fully-successful migration) the
        ``retrodeck_home_path_previous`` marker is deleted in the same
        transaction.
        """
        by_id = {install.rom_id: install for install in installs}
        by_key = {(bf.platform_slug, bf.file_name): bf for bf in bios_files}
        with self._uow_factory() as uow:
            for rom_id, moved in relocations.items():
                install = by_id.get(rom_id)
                if install is None:
                    continue
                install.relocate(moved["rom_dir"], moved["file_path"])
                uow.rom_installs.save(install)
            for key, new_path in bios_relocations.items():
                bios_file = by_key.get(key)
                if bios_file is None:
                    continue
                bios_file.relocate(new_path)
                uow.bios_files.save(bios_file)
            if clear_marker:
                uow.kv_config.delete(_KV_RETRODECK_HOME_PREVIOUS)

    async def migrate_retrodeck_files(self, conflict_strategy=None):
        """Move downloaded ROMs, BIOS, and save files from old RetroDECK path to new.

        Args:
            conflict_strategy: None to scan and return conflicts, "overwrite" to
                replace existing destination files, "skip" to keep existing files
                and just update state paths.
        """
        with self._uow_factory() as uow:
            old_home = uow.kv_config.get(_KV_RETRODECK_HOME_PREVIOUS) or ""
            new_home = uow.kv_config.get(_KV_RETRODECK_HOME) or ""

        if not old_home or not new_home or old_home == new_home:
            return {"success": False, "message": "No path migration needed"}

        return await self._loop.run_in_executor(
            None, self._migrate_retrodeck_files_io, old_home, new_home, conflict_strategy
        )

    def _get_migration_status_io(self, old_home, new_home):
        """Sync helper for get_migration_status — FS traversal in executor."""
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
            bios_files = list(uow.bios_files.iter_all())
        items = self._collect_migration_items(old_home, new_home, installs, bios_files, {}, {})
        roms_count = sum(1 for _, _, _, _, kind in items if kind in ("rom", "rom_dir"))
        bios_count = sum(1 for _, _, _, _, kind in items if kind == "bios")
        saves_count = sum(1 for _, _, _, _, kind in items if kind == "save")

        return {
            "pending": True,
            "old_path": old_home,
            "new_path": new_home,
            "roms_count": roms_count,
            "bios_count": bios_count,
            "saves_count": saves_count,
        }

    async def get_migration_status(self):
        """Return whether a RetroDECK path migration is pending and file counts."""
        with self._uow_factory() as uow:
            old_home = uow.kv_config.get(_KV_RETRODECK_HOME_PREVIOUS) or ""
            new_home = uow.kv_config.get(_KV_RETRODECK_HOME) or ""

        if not old_home or not new_home or old_home == new_home:
            return {"pending": False}

        return await self._loop.run_in_executor(None, self._get_migration_status_io, old_home, new_home)

    # ---------------------------------------------------------------------------
    # Save sort change detection and migration
    # ---------------------------------------------------------------------------

    @staticmethod
    def _read_save_sort_settings(uow) -> SaveSortSettings | None:
        """Decode the last-seen save-sort observation from kv_config, ``None`` when absent."""
        raw = uow.kv_config.get(_KV_SAVE_SORT)
        return json.loads(raw) if raw is not None else None

    @staticmethod
    def _read_save_sort_settings_previous(uow) -> SaveSortSettings | None:
        """Decode the pending pre-change save-sort snapshot from kv_config, ``None`` when absent."""
        raw = uow.kv_config.get(_KV_SAVE_SORT_PREVIOUS)
        return json.loads(raw) if raw is not None else None

    def detect_save_sort_change(self) -> None:
        """Check if RetroArch save sorting settings changed since last run.

        May be called from a worker thread (via
        ``SaveService._refresh_save_sort_state`` → ``run_in_executor``) or
        from the loop thread. Use ``asyncio.run_coroutine_threadsafe`` to
        schedule the emit coroutine: it is explicitly thread-safe and
        also works correctly when invoked from the loop thread itself.
        ``loop.create_task`` is NOT thread-safe and races with loop
        internals on CPython (#238 review).
        """
        sort_by_content, sort_by_core = self._get_retroarch_save_sorting()
        current: SaveSortSettings = {"sort_by_content": sort_by_content, "sort_by_core": sort_by_core}
        with self._uow_factory() as uow:
            stored = self._read_save_sort_settings(uow)
        if stored is None:
            with self._uow_factory() as uow:
                uow.kv_config.set(_KV_SAVE_SORT, json.dumps(current))
            return
        if stored == current:
            return
        with self._uow_factory() as uow:
            uow.kv_config.set(_KV_SAVE_SORT_PREVIOUS, json.dumps(stored))
            uow.kv_config.set(_KV_SAVE_SORT, json.dumps(current))
        self._logger.warning(f"RetroArch save sorting changed: {stored} -> {current}")
        # Fire-and-forget: thread-safe schedule of the emit coroutine on
        # the plugin event loop. We deliberately do not await or .result()
        # the future — this mirrors the previous create_task semantics.
        asyncio.run_coroutine_threadsafe(
            self._emit(
                "save_sort_changed",
                {"old_settings": stored, "new_settings": current},
            ),
            self._loop,
        )

    def _resolve_retroarch_corename(self, system: str, rom_filename: str) -> tuple[str | None, str | None]:
        """Resolve the RetroArch save subdirectory name for a system/ROM.

        Asks ES-DE (via ``get_active_core``) **which** core is active,
        then asks the RetroArch ``.info`` parser (via ``get_core_name``)
        **what** RetroArch calls that core in its own subsystem — which
        is what ``sort_savefiles_enable`` uses when naming save
        subdirectories.

        Returns a ``(corename, core_so)`` tuple. ``corename`` is ``None``
        (fail loud, no ES-DE label fallback) when the providers cannot
        resolve a core for this system/ROM. ``core_so`` is the underlying
        ES-DE core ``.so`` basename when known (useful for diagnostics
        when ``corename`` is ``None``), otherwise ``None``.
        """
        core_so, _label = self._get_active_core(system, rom_filename)
        if not core_so:
            return (None, None)
        corename = self._get_core_name(core_so)
        return (corename or None, core_so)

    def _collect_save_sorting_items(
        self,
        old_settings: SaveSortSettings,
        new_settings: SaveSortSettings,
        installs: list[RomInstall],
    ) -> list[tuple[str, str, str, object, str]]:
        """Collect save files that need migration due to sort setting change.

        ``installs`` is the pre-snapshotted ``RomInstall`` list (the caller opens
        the read UoW); this method is pure compute over it.
        """
        saves_base = self._retrodeck_paths.saves_path()
        roms_base = self._retrodeck_paths.roms_path()
        need_core = bool(old_settings.get("sort_by_core") or new_settings.get("sort_by_core"))
        items: list[tuple[str, str, str, object, str]] = []
        for install in installs:
            self._collect_rom_sort_items(
                install,
                saves_base,
                roms_base,
                old_settings,
                new_settings,
                need_core,
                items,
            )
        return items

    def _collect_rom_sort_items(
        self,
        install: RomInstall,
        saves_base: str,
        roms_base: str,
        old_settings: SaveSortSettings,
        new_settings: SaveSortSettings,
        need_core: bool,
        items: list[tuple[str, str, str, object, str]],
    ) -> None:
        """Collect migration items for a single ROM's save files."""
        system = install.system
        file_path = install.file_path
        platform_slug = install.platform_slug
        if not system or not file_path:
            return
        core_name: str | None = None
        if need_core:
            core_name, core_so = self._resolve_retroarch_corename(system, os.path.basename(file_path))
            if core_name is None:
                # Fail loud — cannot resolve the RetroArch corename for this ROM's
                # active core, so we can't build the correct sort-by-core path.
                # Skip this item and warn the user rather than silently corrupting
                # the migration with the wrong destination directory.
                self._logger.warning(
                    "Skipping save sort migration for %s/%s: unable to resolve "
                    "RetroArch corename from .info (core_so=%s)",
                    system,
                    os.path.basename(file_path),
                    core_so,
                )
                return
        old_dir = resolve_save_dir(
            file_path,
            saves_base,
            system,
            roms_base=roms_base,
            sort_by_content=old_settings["sort_by_content"],
            sort_by_core=old_settings["sort_by_core"],
            core_name=core_name,
        )
        new_dir = resolve_save_dir(
            file_path,
            saves_base,
            system,
            roms_base=roms_base,
            sort_by_content=new_settings["sort_by_content"],
            sort_by_core=new_settings["sort_by_core"],
            core_name=core_name,
        )
        if old_dir == new_dir:
            return
        rom_name = os.path.splitext(os.path.basename(file_path))[0]
        for ext in get_save_extensions(platform_slug):
            filename = rom_name + ext
            old_file = os.path.join(old_dir, filename)
            new_file = os.path.join(new_dir, filename)
            if self._migration_file_store.exists(old_file):
                items.append((filename, old_file, new_file, lambda: None, "save"))

    def _get_save_sort_migration_status_io(
        self, old_settings: SaveSortSettings, new_settings: SaveSortSettings
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
        items = self._collect_save_sorting_items(old_settings, new_settings, installs)
        return {
            "pending": True,
            "old_settings": old_settings,
            "new_settings": new_settings,
            "saves_count": len(items),
        }

    def dismiss_save_sort_migration(self) -> dict[str, Any]:
        """Dismiss the save sort migration warning without migrating files."""
        with self._uow_factory() as uow:
            uow.kv_config.delete(_KV_SAVE_SORT_PREVIOUS)
        return {"success": True}

    async def get_save_sort_migration_status(self) -> dict[str, Any]:
        with self._uow_factory() as uow:
            old = self._read_save_sort_settings_previous(uow)
            new = self._read_save_sort_settings(uow)
        if not old or not new or old == new:
            return {"pending": False}
        return await self._loop.run_in_executor(None, self._get_save_sort_migration_status_io, old, new)

    async def refresh_state(self) -> dict[str, Any]:
        """Run both detection passes and return combined migration state.

        Detects any RetroDECK home-path change and any RetroArch save-sort
        change, then returns the current status of both migrations.
        """
        self.detect_retrodeck_path_change()
        self.detect_save_sort_change()
        return {
            "retrodeck": await self.get_migration_status(),
            "save_sort": await self.get_save_sort_migration_status(),
        }

    def _resolve_save_sort_conflict(
        self,
        label: str,
        old_path: str,
        new_path: str,
        state_updater,
        counts: dict[str, int],
        count_key: str,
        errors: list[str],
    ) -> None:
        """Newest-wins resolution for a save-sort conflict.

        RetroArch does not migrate saves when its sort setting changes. If a
        user flips ``sort_savefiles_enable`` mid-game via the Quick Menu and
        then saves in-game, the new progress is written to the new layout
        while the old location still holds pre-change content. The file at
        the newer mtime contains actual user progress; the older one is
        stale and must be cleaned up. Save-sync has already uploaded the
        newest version to RomM before this runs, so even if local migration
        fails the server still holds the authoritative copy.
        """
        try:
            old_mtime = self._migration_file_store.get_mtime(old_path)
            new_mtime = self._migration_file_store.get_mtime(new_path)
        except OSError as e:
            errors.append(f"{label}: {e}")
            self._logger.error(f"Save-sort conflict mtime read failed: {old_path}: {e}")
            return

        if new_mtime >= old_mtime:
            # Destination is newer — keep it, delete the stale orphan at old_path.
            try:
                self._migration_file_store.remove_file(old_path)
                state_updater()
                counts[count_key] = counts.get(count_key, 0) + 1
                self._logger.info(f"Save-sort conflict: kept newer {new_path}, removed stale {old_path}")
            except OSError as e:
                errors.append(f"{label}: {e}")
                self._logger.error(f"Save-sort orphan cleanup failed: {old_path}: {e}")
            return

        # Source is newer — atomically overwrite destination.
        try:
            self._migration_file_store.make_dirs(os.path.dirname(new_path))
            self._migration_file_store.rename(old_path, new_path)
            state_updater()
            counts[count_key] = counts.get(count_key, 0) + 1
            self._logger.info(f"Save-sort conflict: moved newer {old_path} -> {new_path}")
        except OSError as e:
            errors.append(f"{label}: {e}")
            self._logger.error(f"Save-sort overwrite failed: {old_path}: {e}")

    def _migrate_save_sort_files_io(
        self, old_settings: SaveSortSettings, new_settings: SaveSortSettings, conflict_strategy: str | None
    ) -> dict[str, Any]:
        # conflict_strategy is retained for backwards-compatibility with the
        # callable signature but is unused for save-sort migration — conflicts
        # are resolved in place via newest-wins (see _resolve_save_sort_conflict).
        del conflict_strategy
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
        items = self._collect_save_sorting_items(old_settings, new_settings, installs)
        if not items:
            with self._uow_factory() as uow:
                uow.kv_config.delete(_KV_SAVE_SORT_PREVIOUS)
            return {"success": True, "message": "No save files to migrate", "saves_moved": 0}
        counts: dict[str, int] = {"rom": 0, "bios": 0, "save": 0}
        errors: list[str] = []
        for label, old_path, new_path, updater, _kind in items:
            if self._migration_file_store.exists(old_path) and self._migration_file_store.exists(new_path):
                self._resolve_save_sort_conflict(label, old_path, new_path, updater, counts, "save", errors)
            else:
                self._migrate_single_item(label, old_path, new_path, updater, "save", None, counts, errors)
        if not errors:
            with self._uow_factory() as uow:
                uow.kv_config.delete(_KV_SAVE_SORT_PREVIOUS)
        return self._build_migration_result(counts, errors)

    async def migrate_save_sort_files(self, conflict_strategy: str | None = None) -> dict[str, Any]:
        with self._uow_factory() as uow:
            old = self._read_save_sort_settings_previous(uow)
            new = self._read_save_sort_settings(uow)
        if not old or not new or old == new:
            return {"success": False, "message": "No save sorting migration needed"}
        return await self._loop.run_in_executor(None, self._migrate_save_sort_files_io, old, new, conflict_strategy)
