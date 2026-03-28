"""MigrationService — RetroDECK path migration.

Detects when RetroDECK home path changes (e.g., internal SSD to SD card)
and migrates downloaded ROMs, BIOS files, and save files to the new location.
Also detects RetroArch save sorting setting changes and migrates affected save files.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from domain.save_extensions import get_save_extensions
from domain.save_path import resolve_save_dir

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import (
        BiosPathProvider,
        CoreResolverFn,
        EventEmitter,
        RetroArchSaveSortingProvider,
        RetroDeckHomeProvider,
        RomsPathProvider,
        SavesPathProvider,
        StatePersister,
    )


class MigrationService:
    """Handles RetroDECK path change detection and file migration."""

    def __init__(
        self,
        *,
        state: dict,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        save_state: StatePersister,
        emit: EventEmitter,
        get_bios_files_index: Callable[[], dict],
        get_retrodeck_home: RetroDeckHomeProvider | None = None,
        get_saves_path: SavesPathProvider | None = None,
        get_bios_path: BiosPathProvider | None = None,
        get_retroarch_save_sorting: RetroArchSaveSortingProvider | None = None,
        get_roms_path: RomsPathProvider | None = None,
        get_active_core: CoreResolverFn | None = None,
    ) -> None:
        self._state = state
        self._loop = loop
        self._logger = logger
        self._save_state = save_state
        self._emit = emit
        self._get_bios_files_index = get_bios_files_index
        self._get_retrodeck_home = get_retrodeck_home
        self._get_saves_path = get_saves_path
        self._get_bios_path = get_bios_path
        self._get_retroarch_save_sorting = get_retroarch_save_sorting
        self._get_roms_path = get_roms_path
        self._get_active_core = get_active_core

    def detect_retrodeck_path_change(self) -> None:
        """Check if RetroDECK home path changed since last run."""
        current_home = self._get_retrodeck_home() if self._get_retrodeck_home else ""
        stored_home = self._state.get("retrodeck_home_path", "")

        if not current_home:
            return

        if not os.path.isdir(current_home):
            self._logger.warning(f"RetroDECK home path does not exist, skipping: {current_home}")
            return

        if stored_home == current_home:
            return

        if not stored_home:
            # First run — just store the current path, no migration needed
            self._state["retrodeck_home_path"] = current_home
            self._save_state()
            return

        old_home = stored_home

        # Path changed — store both old and new, emit event
        self._state["retrodeck_home_path_previous"] = old_home
        self._state["retrodeck_home_path"] = current_home
        self._save_state()
        self._logger.warning(f"RetroDECK home path changed: {old_home} -> {current_home}")
        self._loop.create_task(
            self._emit(
                "retrodeck_path_changed",
                {
                    "old_path": old_home,
                    "new_path": current_home,
                },
            )
        )

    def _collect_rom_items(self, old_home, new_home):
        """Collect ROM migration items from installed_roms state."""
        items = []
        for entry in self._state["installed_roms"].values():
            for key in ("file_path", "rom_dir"):
                path = entry.get(key, "")
                if not path or not path.startswith(old_home + os.sep):
                    continue
                new_path = os.path.join(new_home, os.path.relpath(path, old_home))

                def make_rom_updater(e, k, np):
                    def update():
                        e[k] = np

                    return update

                items.append(
                    (
                        os.path.basename(path),
                        path,
                        new_path,
                        make_rom_updater(entry, key, new_path),
                        "rom" if key == "file_path" else "rom_dir",
                    )
                )
        return items

    def _collect_tracked_bios_items(self, old_home, new_home):
        """Collect tracked BIOS migration items from downloaded_bios state."""
        items = []
        for file_name, bios_entry in self._state.get("downloaded_bios", {}).items():
            file_path = bios_entry.get("file_path", "")
            if not file_path or not file_path.startswith(old_home + os.sep):
                continue
            new_path = os.path.join(new_home, os.path.relpath(file_path, old_home))

            def make_bios_updater(be, np):
                def update():
                    be["file_path"] = np

                return update

            items.append(
                (
                    file_name,
                    file_path,
                    new_path,
                    make_bios_updater(bios_entry, new_path),
                    "bios",
                )
            )
        return items

    def _collect_untracked_bios_items(self, old_home):
        """Collect untracked BIOS migration items (downloaded before state tracking)."""
        items = []
        old_bios = os.path.join(old_home, "bios")
        new_bios = self._get_bios_path() if self._get_bios_path else ""
        if not os.path.isdir(old_bios):
            return items
        downloaded_bios = self._state.get("downloaded_bios", {})
        for file_name, reg_entry in self._get_bios_files_index().items():
            if file_name in downloaded_bios:
                continue
            firmware_path = reg_entry.get("firmware_path", file_name)
            old_file = os.path.join(old_bios, firmware_path)
            new_file = os.path.join(new_bios, firmware_path)
            if not os.path.exists(old_file):
                continue
            items.append((file_name, old_file, new_file, lambda: None, "bios"))
        return items

    def _collect_save_items(self, old_home):
        """Collect save file migration items by scanning old saves directory."""
        items = []
        old_saves = os.path.join(old_home, "saves")
        new_saves = self._get_saves_path() if self._get_saves_path else ""
        if not os.path.isdir(old_saves):
            return items
        for dirpath, _dirs, filenames in os.walk(old_saves):
            _dirs[:] = [d for d in _dirs if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                old_file = os.path.join(dirpath, fname)
                rel = os.path.relpath(old_file, old_saves)
                new_file = os.path.join(new_saves, rel)
                items.append((rel, old_file, new_file, lambda: None, "save"))
        return items

    def _collect_migration_items(self, old_home, new_home):
        """Collect all files that need migration across ROMs, BIOS, and saves.

        Returns list of (label, old_path, new_path, state_update_fn, kind) tuples.
        state_update_fn is called after a successful move/skip to update state.
        """
        items = []
        items.extend(self._collect_rom_items(old_home, new_home))
        items.extend(self._collect_tracked_bios_items(old_home, new_home))
        items.extend(self._collect_untracked_bios_items(old_home))
        items.extend(self._collect_save_items(old_home))
        return items

    @staticmethod
    def _find_conflicts(items):
        """Return sorted list of labels where both source and destination exist."""
        conflict_set = set()
        for label, old_path, new_path, _updater, _kind in items:
            if os.path.exists(new_path) and os.path.exists(old_path):
                conflict_set.add(label)
        return sorted(conflict_set)

    @staticmethod
    def _build_conflict_details(items: list) -> list[dict]:
        """Return details for items where both source and destination exist."""
        details = []
        for label, old_path, new_path, _updater, _kind in items:
            if os.path.exists(new_path) and os.path.exists(old_path):
                old_stat = os.stat(old_path)
                new_stat = os.stat(new_path)
                details.append(
                    {
                        "filename": label,
                        "old_path": old_path,
                        "old_size": old_stat.st_size,
                        "old_mtime": datetime.fromtimestamp(old_stat.st_mtime, tz=UTC).isoformat(),
                        "new_path": new_path,
                        "new_size": new_stat.st_size,
                        "new_mtime": datetime.fromtimestamp(new_stat.st_mtime, tz=UTC).isoformat(),
                    }
                )
        return sorted(details, key=lambda d: d["filename"])

    def _migrate_single_item(self, label, old_path, new_path, state_updater, kind, conflict_strategy, counts, errors):
        """Migrate a single file/directory item. Updates counts and errors in place."""
        count_key = kind if kind != "rom_dir" else None

        if not os.path.exists(old_path):
            if os.path.exists(new_path):
                state_updater()
                if count_key:
                    counts[count_key] = counts.get(count_key, 0) + 1
            return

        if os.path.exists(new_path):
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
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.move(old_path, new_path)
            state_updater()
            if count_key:
                counts[count_key] = counts.get(count_key, 0) + 1
            self._logger.info(f"Migrated {kind}: {old_path} -> {new_path}")
        except Exception as e:
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
                if os.path.isdir(new_path):
                    shutil.rmtree(new_path)
                else:
                    os.remove(new_path)
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                shutil.move(old_path, new_path)
                state_updater()
                if count_key:
                    counts[count_key] = counts.get(count_key, 0) + 1
            except Exception as e:
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
        items = self._collect_migration_items(old_home, new_home)
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

        # Clear previous path marker after migration
        if not errors:
            self._state.pop("retrodeck_home_path_previous", None)
        self._save_state()

        return self._build_migration_result(counts, errors)

    async def migrate_retrodeck_files(self, conflict_strategy=None):
        """Move downloaded ROMs, BIOS, and save files from old RetroDECK path to new.

        Args:
            conflict_strategy: None to scan and return conflicts, "overwrite" to
                replace existing destination files, "skip" to keep existing files
                and just update state paths.
        """
        old_home = self._state.get("retrodeck_home_path_previous", "")
        new_home = self._state.get("retrodeck_home_path", "")

        if not old_home or not new_home or old_home == new_home:
            return {"success": False, "message": "No path migration needed"}

        return await self._loop.run_in_executor(
            None, self._migrate_retrodeck_files_io, old_home, new_home, conflict_strategy
        )

    def _get_migration_status_io(self, old_home, new_home):
        """Sync helper for get_migration_status — FS traversal in executor."""
        items = self._collect_migration_items(old_home, new_home)
        roms_count = sum(1 for _, _, _, _, kind in items if kind == "rom")
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
        old_home = self._state.get("retrodeck_home_path_previous", "")
        new_home = self._state.get("retrodeck_home_path", "")

        if not old_home or not new_home or old_home == new_home:
            return {"pending": False}

        return await self._loop.run_in_executor(None, self._get_migration_status_io, old_home, new_home)

    # ---------------------------------------------------------------------------
    # Save sort change detection and migration
    # ---------------------------------------------------------------------------

    def detect_save_sort_change(self) -> None:
        """Check if RetroArch save sorting settings changed since last run."""
        if self._get_retroarch_save_sorting is None:
            return
        sort_by_content, sort_by_core = self._get_retroarch_save_sorting()
        current = {"sort_by_content": sort_by_content, "sort_by_core": sort_by_core}
        stored = self._state.get("save_sort_settings")
        if stored is None:
            self._state["save_sort_settings"] = current
            self._save_state()
            return
        if stored == current:
            return
        self._state["save_sort_settings_previous"] = stored
        self._state["save_sort_settings"] = current
        self._save_state()
        self._logger.warning(f"RetroArch save sorting changed: {stored} -> {current}")
        self._loop.create_task(
            self._emit(
                "save_sort_changed",
                {"old_settings": stored, "new_settings": current},
            )
        )

    def _resolve_core_name(self, system: str, rom_filename: str) -> str | None:
        """Resolve the RetroArch core directory name for a system/ROM."""
        if not self._get_active_core:
            return None
        _core_so, label = self._get_active_core(system, rom_filename)
        return label or None

    def _collect_save_sorting_items(self, old_settings: dict, new_settings: dict) -> list:
        """Collect save files that need migration due to sort setting change."""
        if not self._get_saves_path or not self._get_roms_path:
            return []
        saves_base = self._get_saves_path()
        roms_base = self._get_roms_path()
        need_core = bool(old_settings.get("sort_by_core") or new_settings.get("sort_by_core"))
        items: list[tuple[str, str, str, object, str]] = []
        for entry in self._state.get("installed_roms", {}).values():
            self._collect_rom_sort_items(
                entry,
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
        entry: dict,
        saves_base: str,
        roms_base: str,
        old_settings: dict,
        new_settings: dict,
        need_core: bool,
        items: list,
    ) -> None:
        """Collect migration items for a single ROM's save files."""
        system = entry.get("system", "")
        file_path = entry.get("file_path", "")
        platform_slug = entry.get("platform_slug", "")
        if not system or not file_path:
            return
        core_name = self._resolve_core_name(system, os.path.basename(file_path)) if need_core else None
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
            if os.path.exists(old_file):
                items.append((filename, old_file, new_file, lambda: None, "save"))

    def _get_save_sort_migration_status_io(self, old_settings: dict, new_settings: dict) -> dict:
        items = self._collect_save_sorting_items(old_settings, new_settings)
        return {
            "pending": True,
            "old_settings": old_settings,
            "new_settings": new_settings,
            "saves_count": len(items),
        }

    def dismiss_save_sort_migration(self) -> dict:
        """Dismiss the save sort migration warning without migrating files."""
        self._state.pop("save_sort_settings_previous", None)
        self._save_state()
        return {"success": True}

    async def get_save_sort_migration_status(self) -> dict:
        old = self._state.get("save_sort_settings_previous")
        new = self._state.get("save_sort_settings")
        if not old or not new or old == new:
            return {"pending": False}
        return await self._loop.run_in_executor(None, self._get_save_sort_migration_status_io, old, new)

    def _migrate_save_sort_files_io(
        self, old_settings: dict, new_settings: dict, conflict_strategy: str | None
    ) -> dict:
        items = self._collect_save_sorting_items(old_settings, new_settings)
        if not items:
            self._state.pop("save_sort_settings_previous", None)
            self._save_state()
            return {"success": True, "message": "No save files to migrate", "saves_moved": 0}
        if conflict_strategy is None:
            conflict_details = self._build_conflict_details(items)
            if conflict_details:
                return {
                    "success": False,
                    "needs_confirmation": True,
                    "conflict_count": len(conflict_details),
                    "conflicts": conflict_details,
                    "message": f"{len(conflict_details)} save file(s) exist at both old and new locations",
                }
        counts: dict[str, int] = {"rom": 0, "bios": 0, "save": 0}
        errors: list[str] = []
        for label, old_path, new_path, updater, kind in items:
            self._migrate_single_item(label, old_path, new_path, updater, kind, conflict_strategy, counts, errors)
        if not errors:
            self._state.pop("save_sort_settings_previous", None)
            self._save_state()
        return self._build_migration_result(counts, errors)

    async def migrate_save_sort_files(self, conflict_strategy: str | None = None) -> dict:
        old = self._state.get("save_sort_settings_previous")
        new = self._state.get("save_sort_settings")
        if not old or not new or old == new:
            return {"success": False, "message": "No save sorting migration needed"}
        return await self._loop.run_in_executor(None, self._migrate_save_sort_files_io, old, new, conflict_strategy)
