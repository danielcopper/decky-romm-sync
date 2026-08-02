from __future__ import annotations

from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.collection_sync_state import CollectionSyncState
from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom
from domain.version_metadata import VersionMetadata
from services.prune.registry import PruneRegistry, PruneRegistryConfig


def _rom(rom_id: int, *, group: str | None = None, app_id: int | None = None) -> Rom:
    return Rom.synced(
        rom_id=rom_id,
        platform_slug="gba",
        name=f"Game {rom_id}",
        fs_name=f"Game {rom_id}.gba",
        shortcut_app_id=app_id,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=group),
    )


def _registry(uow: FakeUnitOfWork) -> PruneRegistry:
    return PruneRegistry(config=PruneRegistryConfig(uow_factory=FakeUnitOfWorkFactory(uow)))


def test_candidate_groups_keep_null_keys_as_singletons() -> None:
    uow = FakeUnitOfWork()
    with uow:
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        uow.roms.save(_rom(3, group="g"))
        uow.roms.save(_rom(4, group="g"))

    groups = _registry(uow).groups_for_candidates({1, 3})

    assert [[row.rom_id for row in group] for group in groups] == [[1], [3, 4]]


def test_repointed_group_delete_revalidates_and_invalidates_collection_only() -> None:
    uow = FakeUnitOfWork()
    app_id = 0x80000001
    old = _rom(1, group="g", app_id=app_id)
    target = _rom(2, group="g")
    with uow:
        uow.roms.save(old)
        uow.roms.save(target)
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug="gba", at="now", rom_count=2, fetch_id="fetch")
        )
        uow.collection_sync_state.save(
            CollectionSyncState.stamp(
                collection_id="5",
                collection_kind="standard",
                updated_at="server",
                completed_at="now",
                rom_count=2,
                member_rom_ids=(1, 2),
            )
        )
    target.bind_shortcut(app_id)
    with uow:
        uow.roms.save(target)

    current = _registry(uow).reread_group(1)
    deleted = _registry(uow).delete_rows(current, {1}, 2, app_id, False)

    assert deleted is True
    assert uow.roms.get(1) is None
    retained = uow.roms.get(2)
    assert retained is not None
    assert retained.shortcut_app_id == app_id
    assert uow.collection_sync_state.get("5", "standard") is None
    assert uow.platform_sync_state.get("gba") is not None


def test_fully_dead_group_delete_keeps_the_platform_completion_stamp() -> None:
    uow = FakeUnitOfWork()
    with uow:
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug="gba", at="now", rom_count=2, fetch_id="fetch")
        )

    current = _registry(uow).reread_group(1)
    deleted = _registry(uow).delete_rows(current, {1}, None, None, True)

    assert deleted is True
    assert uow.roms.get(1) is None
    stamp = uow.platform_sync_state.get("gba")
    assert stamp is not None
    assert (stamp.fetch_id, stamp.rom_count) == ("fetch", 2)


def test_new_sibling_or_changed_binding_blocks_pre_mutation_validation() -> None:
    uow = FakeUnitOfWork()
    app_id = 0x80000001
    expected = [_rom(1, group="g", app_id=app_id), _rom(2, group="g")]
    with uow:
        for row in expected:
            uow.roms.save(row)
        uow.roms.save(_rom(3, group="g"))

    assert _registry(uow).validate_deletion_state(expected, {1}, 2, app_id, False) is False


def _stamped(uow: FakeUnitOfWork, slug: str, fetch_id: str, rom_count: int = 1) -> None:
    with uow:
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug=slug, at="now", rom_count=rom_count, fetch_id=fetch_id)
        )


def _generation_rom(rom_id: int, slug: str, fetch_id: str | None) -> Rom:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug=slug,
        name=f"Game {rom_id}",
        fs_name=f"Game {rom_id}.gba",
        shortcut_app_id=None,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=None),
    )
    if fetch_id is not None:
        rom.record_fetch_generation(fetch_id)
    return rom


class TestCanaryRomIds:
    def test_returns_only_ids_the_last_complete_fetch_returned(self) -> None:
        uow = FakeUnitOfWork()
        with uow:
            uow.roms.save(_generation_rom(1, "gba", "old"))
            uow.roms.save(_generation_rom(2, "gba", "new"))
            uow.roms.save(_generation_rom(3, "gba", None))
        _stamped(uow, "gba", "new")

        assert _registry(uow).canary_rom_ids(set(), 5) == [2]

    def test_excludes_the_ids_under_question(self) -> None:
        uow = FakeUnitOfWork()
        with uow:
            uow.roms.save(_generation_rom(2, "gba", "new"))
            uow.roms.save(_generation_rom(4, "gba", "new"))
        _stamped(uow, "gba", "new")

        assert _registry(uow).canary_rom_ids({2}, 5) == [4]

    def test_is_deterministic_and_capped(self) -> None:
        uow = FakeUnitOfWork()
        with uow:
            for rom_id in (9, 5, 7, 3):
                uow.roms.save(_generation_rom(rom_id, "gba", "new"))
        _stamped(uow, "gba", "new", rom_count=4)

        assert _registry(uow).canary_rom_ids(set(), 2) == [3, 5]

    def test_spans_platforms_each_judged_by_its_own_stamp(self) -> None:
        uow = FakeUnitOfWork()
        with uow:
            uow.roms.save(_generation_rom(1, "gba", "gba-new"))
            uow.roms.save(_generation_rom(2, "snes", "snes-old"))
            uow.roms.save(_generation_rom(3, "snes", "snes-new"))
        _stamped(uow, "gba", "gba-new")
        _stamped(uow, "snes", "snes-new")

        assert _registry(uow).canary_rom_ids(set(), 5) == [1, 3]

    def test_a_platform_without_a_usable_stamp_offers_nothing(self) -> None:
        uow = FakeUnitOfWork()
        with uow:
            uow.roms.save(_generation_rom(1, "gba", "new"))

        assert _registry(uow).canary_rom_ids(set(), 5) == []

    def test_an_empty_library_offers_nothing(self) -> None:
        assert _registry(FakeUnitOfWork()).canary_rom_ids(set(), 5) == []
