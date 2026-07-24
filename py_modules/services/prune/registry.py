"""Local registry reads, race validation, and final aggregate deletion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.prune import group_rows

if TYPE_CHECKING:
    from domain.rom import Rom
    from services.protocols import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True)
class PruneRegistryConfig:
    """Persistence dependency for cleanup registry operations."""

    uow_factory: UnitOfWorkFactory


class PruneRegistry:
    """Own the short SQLite reads and final delete for a cleanup run."""

    def __init__(self, *, config: PruneRegistryConfig) -> None:
        self._uow_factory = config.uow_factory

    def groups_for_candidates(self, candidate_ids: set[int]) -> list[list[Rom]]:
        with self._uow_factory() as uow:
            rows = list(uow.roms.iter_all())
        return [group for group in group_rows(rows) if candidate_ids.intersection(row.rom_id for row in group)]

    def reread_group(self, rom_id: int) -> list[Rom]:
        with self._uow_factory() as uow:
            row = uow.roms.get(rom_id)
            if row is None:
                return []
            return list(uow.roms.iter_by_group_key(row.sibling_group_key)) if row.sibling_group_key else [row]

    def validate_deletion_state(
        self,
        expected_rows: list[Rom],
        delete_ids: set[int],
        target_id: int | None,
        app_id: int | None,
        fully_dead: bool,
    ) -> bool:
        with self._uow_factory() as uow:
            return self._deletion_state_matches(uow, expected_rows, delete_ids, target_id, app_id, fully_dead)

    def delete_rows(
        self,
        expected_rows: list[Rom],
        delete_ids: set[int],
        target_id: int | None,
        app_id: int | None,
        fully_dead: bool,
    ) -> bool:
        with self._uow_factory() as uow:
            if not self._deletion_state_matches(uow, expected_rows, delete_ids, target_id, app_id, fully_dead):
                return False
            for stamp in list(uow.collection_sync_state.iter_all()):
                if delete_ids.intersection(stamp.member_rom_ids):
                    uow.collection_sync_state.delete(stamp.collection_id, stamp.collection_kind)
            if fully_dead:
                for slug in {row.platform_slug for row in expected_rows if row.rom_id in delete_ids}:
                    uow.platform_sync_state.delete(slug)
            for rom_id in delete_ids:
                uow.roms.delete(rom_id)
        return True

    @staticmethod
    def _deletion_state_matches(
        uow: UnitOfWork,
        expected_rows: list[Rom],
        delete_ids: set[int],
        target_id: int | None,
        app_id: int | None,
        fully_dead: bool,
    ) -> bool:
        if not expected_rows:
            return False
        expected = {row.rom_id: row for row in expected_rows}
        if not delete_ids <= expected.keys():
            return False
        group_key = expected_rows[0].sibling_group_key
        if group_key is not None:
            current_group = list(uow.roms.iter_by_group_key(group_key))
            if {row.rom_id for row in current_group} != expected.keys():
                return False
        else:
            singleton = uow.roms.get(expected_rows[0].rom_id)
            if singleton is None:
                return False
            current_group = [singleton]
        expected_app_ids = {row.shortcut_app_id for row in expected_rows if row.shortcut_app_id is not None}
        current_app_ids = {row.shortcut_app_id for row in current_group if row.shortcut_app_id is not None}
        if current_app_ids != expected_app_ids or sum(row.shortcut_app_id is not None for row in current_group) > 1:
            return False
        current_rows: list[Rom] = []
        for rom_id in delete_ids:
            row = uow.roms.get(rom_id)
            if row is None or row.sibling_group_key != expected[rom_id].sibling_group_key:
                return False
            current_rows.append(row)
        bound_app_ids = {row.shortcut_app_id for row in current_rows if row.shortcut_app_id is not None}
        if target_id is not None:
            target = uow.roms.get(target_id)
            return target is not None and target.shortcut_app_id == app_id and not bound_app_ids
        if fully_dead:
            return bound_app_ids == ({app_id} if app_id is not None else set())
        return not bound_app_ids
