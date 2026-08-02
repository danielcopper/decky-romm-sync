"""Local registry reads, race validation, and final aggregate deletion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.fetch_generation import current_generation_ids
from domain.prune import group_rows

if TYPE_CHECKING:
    from domain.rom import Rom
    from services.protocols import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True)
class PruneRegistryConfig:
    """Persistence dependency for cleanup registry operations."""

    uow_factory: UnitOfWorkFactory


def _bindings_unchanged(expected_rows: list[Rom], current_group: list[Rom]) -> bool:
    """Whether the group still carries exactly the shortcut bindings it was read with.

    More than one binding is refused outright: the run's whole model is that a
    group owns at most one shortcut, and a second one appearing means something
    else has been editing the registry.
    """
    expected_app_ids = {row.shortcut_app_id for row in expected_rows if row.shortcut_app_id is not None}
    current_app_ids = {row.shortcut_app_id for row in current_group if row.shortcut_app_id is not None}
    if current_app_ids != expected_app_ids:
        return False
    return sum(row.shortcut_app_id is not None for row in current_group) <= 1


class PruneRegistry:
    """Own the short SQLite reads and final delete for a cleanup run."""

    def __init__(self, *, config: PruneRegistryConfig) -> None:
        self._uow_factory = config.uow_factory

    def groups_for_candidates(self, candidate_ids: set[int]) -> list[list[Rom]]:
        with self._uow_factory() as uow:
            rows = list(uow.roms.iter_all())
        return [group for group in group_rows(rows) if candidate_ids.intersection(row.rom_id for row in group)]

    def canary_rom_ids(self, exclude: set[int], limit: int) -> list[int]:
        """Return rom ids the last complete fetch returned, as controls for a 404 round.

        These are the ids RomM served most recently, so asking for one is the
        cheapest available test of whether the ROM endpoint is still answering
        for ids that exist. Deterministic (ascending) so the audit trail names
        the same subject a re-run would pick, and capped by *limit* because this
        is a control, not a survey.
        """
        with self._uow_factory() as uow:
            rows = list(uow.roms.iter_all())
            served: set[int] = set()
            for slug in sorted({row.platform_slug for row in rows}):
                platform_rows = [row for row in rows if row.platform_slug == slug]
                served |= current_generation_ids(platform_rows, uow.platform_sync_state.get(slug))
        return sorted(served - exclude)[:limit]

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

    def validate_action_state(
        self,
        kind: str,
        expected_bound_rom_id: int,
        app_id: int,
        target_id: int | None,
        group_rom_ids: frozenset[int],
    ) -> bool:
        """Require the exact action binding and one-binding group immediately before claim."""
        with self._uow_factory() as uow:
            anchor = uow.roms.get(expected_bound_rom_id)
            if anchor is None:
                return False
            current = (
                list(uow.roms.iter_by_group_key(anchor.sibling_group_key))
                if anchor.sibling_group_key is not None
                else [anchor]
            )
            if {row.rom_id for row in current} != set(group_rom_ids):
                return False
            bindings = [row for row in current if row.shortcut_app_id is not None]
            if len(bindings) != 1 or bindings[0].shortcut_app_id != app_id:
                return False
            bound = uow.roms.get_by_app_id(app_id)
            if bound is None:
                return False
            expected_id = target_id if kind == "repoint_shortcut" else expected_bound_rom_id
            return bound.rom_id == expected_id

    def reconcile_removed_shortcut(self, expected_bound_rom_id: int, app_id: int) -> bool:
        """Persist Steam's confirmed shortcut absence without deleting source data."""
        with self._uow_factory() as uow:
            bound = uow.roms.get_by_app_id(app_id)
            if bound is None or bound.rom_id != expected_bound_rom_id:
                return False
            bound.unbind_shortcut()
            uow.roms.save(bound)
        return True

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
            # Selected before any deletion: the repository hands back an
            # Iterator, which promises nothing about surviving a mutation of the
            # collection it walks.
            orphaned = [
                stamp for stamp in uow.collection_sync_state.iter_all() if delete_ids.intersection(stamp.member_rom_ids)
            ]
            for stamp in orphaned:
                uow.collection_sync_state.delete(stamp.collection_id, stamp.collection_kind)
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
        current_group = PruneRegistry._current_group(uow, expected_rows[0])
        if current_group is None or {row.rom_id for row in current_group} != expected.keys():
            return False
        if not _bindings_unchanged(expected_rows, current_group):
            return False
        current_rows = PruneRegistry._unchanged_rows(uow, delete_ids, expected)
        if current_rows is None:
            return False
        bound_app_ids = {row.shortcut_app_id for row in current_rows if row.shortcut_app_id is not None}
        if target_id is not None:
            target = uow.roms.get(target_id)
            return target is not None and target.shortcut_app_id == app_id and not bound_app_ids
        if fully_dead:
            return bound_app_ids == ({app_id} if app_id is not None else set())
        return not bound_app_ids

    @staticmethod
    def _current_group(uow: UnitOfWork, anchor: Rom) -> list[Rom] | None:
        """The group as it stands now, read the same way it was grouped originally."""
        if anchor.sibling_group_key is not None:
            return list(uow.roms.iter_by_group_key(anchor.sibling_group_key))
        singleton = uow.roms.get(anchor.rom_id)
        return None if singleton is None else [singleton]

    @staticmethod
    def _unchanged_rows(uow: UnitOfWork, delete_ids: set[int], expected: dict[int, Rom]) -> list[Rom] | None:
        """Every row to be deleted, re-read, or ``None`` if any of them moved."""
        current_rows: list[Rom] = []
        for rom_id in delete_ids:
            row = uow.roms.get(rom_id)
            if row is None or row != expected[rom_id]:
                return None
            current_rows.append(row)
        return current_rows
