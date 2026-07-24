"""Tests for ``SqliteRomRepository`` over the ``roms`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.playtime import Playtime
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_metadata import RomMetadata
from domain.rom_save_sync_state import RomSaveSyncState

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _rom(rom_id: int, *, platform: str = "snes", app_id: int = 1000) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug=platform,
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.sfc",
        shortcut_app_id=app_id,
        last_synced_at="2026-01-01T00:00:00Z",
    )


class TestRoundTrip:
    def test_all_fields_preserved_with_optionals_set(self, uow: SqliteUnitOfWork):
        rom = Rom(
            rom_id=42,
            platform_slug="gba",
            name="Pokemon",
            fs_name="pokemon.gba",
            shortcut_app_id=98765,
            last_synced_at="2026-05-01T12:00:00Z",
            cover_path="/covers/42.png",
            cover_source="/assets/romm/resources/roms/42/cover/big.png?ts=2025-07-28 00:05:03",
            igdb_id=111,
            sgdb_id=222,
            ra_id=333,
        )
        uow.roms.save(rom)

        loaded = uow.roms.get(42)
        assert loaded == rom

    def test_null_optionals_preserved(self, uow: SqliteUnitOfWork):
        rom = _rom(7)
        uow.roms.save(rom)

        loaded = uow.roms.get(7)
        assert loaded is not None
        assert loaded.cover_path is None
        assert loaded.cover_source is None
        assert loaded.igdb_id is None
        assert loaded.sgdb_id is None
        assert loaded.ra_id is None


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.roms.get(999) is None

    def test_get_by_app_id_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.roms.get_by_app_id(123) is None


class TestGetByAppId:
    def test_finds_rom_by_shortcut_app_id(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, app_id=5000))
        uow.roms.save(_rom(2, app_id=6000))

        found = uow.roms.get_by_app_id(6000)
        assert found is not None
        assert found.rom_id == 2


class TestUnboundShortcut:
    def test_null_app_id_round_trips(self, uow: SqliteUnitOfWork):
        rom = _rom(1, app_id=5000)
        rom.unbind_shortcut()
        uow.roms.save(rom)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.shortcut_app_id is None

    def test_get_by_app_id_skips_unbound_rows(self, uow: SqliteUnitOfWork):
        bound = _rom(1, app_id=5000)
        unbound = _rom(2, app_id=6000)
        unbound.unbind_shortcut()
        uow.roms.save(bound)
        uow.roms.save(unbound)

        assert uow.roms.get_by_app_id(5000) is not None
        # The reverse lookup must never resolve a NULL (unbound) row.
        assert uow.roms.get_by_app_id(6000) is None


class TestShortcutAppIdCollision:
    """A new rom_id reusing an old appId (server switch / re-import) must not leave
    two bound rows sharing one appId: save() unbinds the sibling, the 003 partial
    unique index enforces it, and get_by_app_id resolves deterministically (#1036)."""

    def test_collision_save_unbinds_sibling_and_binds_new(self, uow: SqliteUnitOfWork):
        """Re-binding app 5000 to a new rom_id unbinds the old colliding row —
        no IntegrityError against the 003 unique index, one bound row per appId."""
        uow.roms.save(_rom(1, app_id=5000))
        # A new server-issued rom_id resolves to the SAME appId (unchanged exe+name).
        uow.roms.save(_rom(2, app_id=5000))

        old = uow.roms.get(1)
        new = uow.roms.get(2)
        assert old is not None
        assert new is not None
        # Old row survives (ADR-0007) but is unbound; new row holds the appId.
        assert old.shortcut_app_id is None
        assert new.shortcut_app_id == 5000
        # Raw: exactly one bound row carries appId 5000.
        assert uow._conn is not None
        bound = uow._conn.execute("SELECT COUNT(*) FROM roms WHERE shortcut_app_id = 5000").fetchone()[0]
        assert bound == 1

    def test_idempotent_resave_same_rom_keeps_binding(self, uow: SqliteUnitOfWork):
        """Re-saving the SAME rom_id+appId is a no-op for the sibling-unbind guard
        (the rom_id != ? guard never unbinds the row being upserted)."""
        uow.roms.save(_rom(1, app_id=5000))
        uow.roms.save(_rom(1, app_id=5000))

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.shortcut_app_id == 5000
        assert uow.roms.count() == 1

    def test_two_distinct_bound_appids_coexist(self, uow: SqliteUnitOfWork):
        """Distinct appIds are independent — binding one never disturbs the other."""
        uow.roms.save(_rom(1, app_id=5000))
        uow.roms.save(_rom(2, app_id=6000))

        first = uow.roms.get(1)
        second = uow.roms.get(2)
        assert first is not None
        assert second is not None
        assert first.shortcut_app_id == 5000
        assert second.shortcut_app_id == 6000

    def test_multiple_unbound_rows_coexist(self, uow: SqliteUnitOfWork):
        """The partial index allows many NULL-appId rows — saving an unbound ROM
        never triggers the sibling-unbind (no appId to collide on)."""
        r1 = _rom(1, app_id=5000)
        r1.unbind_shortcut()
        r2 = _rom(2, app_id=6000)
        r2.unbind_shortcut()
        uow.roms.save(r1)
        uow.roms.save(r2)

        first = uow.roms.get(1)
        second = uow.roms.get(2)
        assert first is not None
        assert second is not None
        assert first.shortcut_app_id is None
        assert second.shortcut_app_id is None
        assert uow.roms.count() == 2

    def test_get_by_app_id_is_deterministic_newest_wins(self, uow: SqliteUnitOfWork):
        """After the collision-safe re-bind, get_by_app_id resolves the live
        (newest) binding — never the unbound old row."""
        uow.roms.save(_rom(1, app_id=5000))
        uow.roms.save(_rom(2, app_id=5000))

        found = uow.roms.get_by_app_id(5000)
        assert found is not None
        assert found.rom_id == 2

    def test_collision_save_preserves_sibling_children(self, uow: SqliteUnitOfWork):
        """Unbinding the colliding sibling NULLs only its binding — its per-ROM
        children (install/metadata/playtime/saves) survive (ADR-0007, never a DELETE)."""
        uow.roms.save(_rom(1, app_id=5000))
        _seed_children(uow, 1)

        uow.roms.save(_rom(2, app_id=5000))

        # Sibling row + every cascade child still present.
        old = uow.roms.get(1)
        assert old is not None
        assert old.shortcut_app_id is None
        assert uow.rom_installs.get(1) is not None
        assert uow.rom_metadata.get(1) is not None
        assert uow.playtime.get(1) is not None
        assert uow.rom_save_sync_states.get(1) is not None


class TestDelete:
    def test_delete_removes_row(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.delete(1)
        assert uow.roms.get(1) is None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.roms.delete(404)  # no row — must not raise
        assert uow.roms.get(404) is None


class TestIteration:
    def test_iter_all_yields_every_rom(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        uow.roms.save(_rom(3))

        ids = {rom.rom_id for rom in uow.roms.iter_all()}
        assert ids == {1, 2, 3}

    def test_iter_by_platform_returns_subset(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, platform="snes"))
        uow.roms.save(_rom(2, platform="gba"))
        uow.roms.save(_rom(3, platform="snes"))

        snes_ids = {rom.rom_id for rom in uow.roms.iter_by_platform("snes")}
        assert snes_ids == {1, 3}

    def test_count_reflects_row_count(self, uow: SqliteUnitOfWork):
        assert uow.roms.count() == 0
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        assert uow.roms.count() == 2


class TestIterByGroupKey:
    """``iter_by_group_key`` — the sibling-group resolution seam (ADR-0021 #1297)."""

    @staticmethod
    def _keyed(rom_id: int, group_key: str | None) -> Rom:
        return Rom(
            rom_id=rom_id,
            platform_slug="snes",
            name=f"Game {rom_id}",
            fs_name=f"game_{rom_id}.sfc",
            shortcut_app_id=1000 + rom_id,
            last_synced_at="2026-01-01T00:00:00Z",
            sibling_group_key=group_key,
        )

    def test_returns_only_rows_sharing_the_key(self, uow: SqliteUnitOfWork):
        uow.roms.save(self._keyed(1, "igdb:100:57"))
        uow.roms.save(self._keyed(2, "igdb:100:57"))
        uow.roms.save(self._keyed(3, "igdb:999:57"))

        ids = {rom.rom_id for rom in uow.roms.iter_by_group_key("igdb:100:57")}
        assert ids == {1, 2}

    def test_absent_key_returns_empty(self, uow: SqliteUnitOfWork):
        uow.roms.save(self._keyed(1, "igdb:100:57"))
        assert list(uow.roms.iter_by_group_key("igdb:404:57")) == []

    def test_null_key_rows_never_returned(self, uow: SqliteUnitOfWork):
        # A NULL (unbackfilled / solo) key never matches WHERE sibling_group_key = ?.
        uow.roms.save(self._keyed(1, None))
        uow.roms.save(self._keyed(2, "igdb:100:57"))

        assert {rom.rom_id for rom in uow.roms.iter_by_group_key("igdb:100:57")} == {2}


class TestUpsert:
    def test_save_existing_id_overwrites(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, app_id=100))
        uow.roms.save(_rom(1, app_id=200))

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.shortcut_app_id == 200
        assert uow.roms.count() == 1


class TestCoverSource:
    """``cover_source`` is a sync column (#1386): save() writes the aggregate's
    value on every UPSERT — the confirmed-else-preserved merge happens on the Rom
    upstream, not here — so it is NOT pin-preserved like emulator_override."""

    def test_rides_the_upsert(self, uow: SqliteUnitOfWork):
        first = _rom(1)
        first.adopt_cover_source("/cover/big.png?ts=2026-01-01 00:00:00")
        uow.roms.save(first)

        resaved = _rom(1)
        resaved.adopt_cover_source("/cover/big.png?ts=2026-07-11 12:00:00")
        uow.roms.save(resaved)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.cover_source == "/cover/big.png?ts=2026-07-11 12:00:00"

    def test_resave_without_source_writes_null(self, uow: SqliteUnitOfWork):
        # A fresh Rom (cover_source None) re-saved over a fingerprinted row NULLs
        # the column — the merge protecting against this lives in the reporter's
        # commit, which is why every write path must construct via that merge.
        first = _rom(1)
        first.adopt_cover_source("/cover/big.png?ts=2026-01-01 00:00:00")
        uow.roms.save(first)

        uow.roms.save(_rom(1))

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.cover_source is None


class TestVersionMetadata:
    """The sibling-group key + version dimensions round-trip through JSON columns
    and — being server-derived (ADR-0019) — REFRESH on a re-sync (the opposite of
    the user-pin columns)."""

    def test_round_trips_all_version_fields(self, uow: SqliteUnitOfWork):
        rom = Rom(
            rom_id=1,
            platform_slug="snes",
            name="Chrono Trigger",
            fs_name="ct.sfc",
            shortcut_app_id=1000,
            last_synced_at="2026-01-01T00:00:00Z",
            sibling_group_key="igdb:3404:57",
            regions=("USA", "Europe"),
            languages=("En", "Fr"),
            revision="1",
            tags=("Demo",),
            is_main_sibling=True,
        )
        uow.roms.save(rom)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded == rom
        # The JSON columns decode back to tuples, not lists.
        assert loaded.regions == ("USA", "Europe")
        assert loaded.languages == ("En", "Fr")
        assert loaded.tags == ("Demo",)
        assert loaded.is_main_sibling is True

    def test_defaults_when_absent(self, uow: SqliteUnitOfWork):
        # A Rom built with no version metadata (the aggregate defaults) reads back
        # empty tuples / blank revision / False / NULL group key.
        uow.roms.save(_rom(1))
        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.sibling_group_key is None
        assert loaded.regions == ()
        assert loaded.languages == ()
        assert loaded.revision == ""
        assert loaded.tags == ()
        assert loaded.is_main_sibling is False

    def test_resync_refreshes_version_metadata(self, uow: SqliteUnitOfWork):
        # Version metadata rides the sync UPSERT — a re-sync with new server facts
        # OVERWRITES the prior values (a sibling joining/leaving a group, a region
        # re-tag). Contrast TestResyncPreservesOverride, which pins user columns.
        v1 = Rom(
            rom_id=1,
            platform_slug="snes",
            name="Game",
            fs_name="game.sfc",
            shortcut_app_id=100,
            last_synced_at="2026-01-01T00:00:00Z",
            sibling_group_key="romm:1:57",
            regions=("Japan",),
            revision="0",
            is_main_sibling=False,
        )
        uow.roms.save(v1)

        v2 = Rom(
            rom_id=1,
            platform_slug="snes",
            name="Game",
            fs_name="game.sfc",
            shortcut_app_id=100,
            last_synced_at="2026-02-01T00:00:00Z",
            sibling_group_key="igdb:3404:57",
            regions=("USA", "Europe"),
            languages=("En",),
            revision="1",
            tags=("Rev A",),
            is_main_sibling=True,
        )
        uow.roms.save(v2)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.sibling_group_key == "igdb:3404:57"
        assert loaded.regions == ("USA", "Europe")
        assert loaded.languages == ("En",)
        assert loaded.revision == "1"
        assert loaded.tags == ("Rev A",)
        assert loaded.is_main_sibling is True

    def test_backfill_null_group_key_becomes_populated_on_resync(self, uow: SqliteUnitOfWork):
        # A pre-migration row (NULL group key) is backfilled when a later sync
        # re-saves it with a computed key.
        uow.roms.save(_rom(1))
        before = uow.roms.get(1)
        assert before is not None
        assert before.sibling_group_key is None

        refreshed = Rom(
            rom_id=1,
            platform_slug="snes",
            name="Game 1",
            fs_name="game_1.sfc",
            shortcut_app_id=1000,
            last_synced_at="2026-01-01T00:00:00Z",
            sibling_group_key="igdb:99:5",
        )
        uow.roms.save(refreshed)

        after = uow.roms.get(1)
        assert after is not None
        assert after.sibling_group_key == "igdb:99:5"


class TestFsSizeBytes:
    """``fs_size_bytes`` is a sync column (#1395): the server-reported ROM size
    rides the UPSERT and refreshes on every sync (like the version dimensions),
    and ``set_fs_size_bytes`` is the between-syncs download write-back."""

    def test_round_trips_via_get(self, uow: SqliteUnitOfWork):
        rom = _rom(1)
        rom.fs_size_bytes = 3_145_728
        uow.roms.save(rom)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.fs_size_bytes == 3_145_728

    def test_none_round_trips_as_null(self, uow: SqliteUnitOfWork):
        # A fresh Rom (no size) reads back NULL — the "unknown" state the frontend
        # hides on.
        uow.roms.save(_rom(1))
        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.fs_size_bytes is None
        assert uow._conn is not None
        stored = uow._conn.execute("SELECT fs_size_bytes FROM roms WHERE rom_id = 1").fetchone()[0]
        assert stored is None

    def test_set_fs_size_bytes_updates_persisted_row(self, uow: SqliteUnitOfWork):
        # The download write-back: a persisted row's size is topped up in place.
        uow.roms.save(_rom(1))
        uow.roms.set_fs_size_bytes(1, 8_388_608)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.fs_size_bytes == 8_388_608

    def test_set_fs_size_bytes_none_writes_sql_null(self, uow: SqliteUnitOfWork):
        rom = _rom(1)
        rom.fs_size_bytes = 1234
        uow.roms.save(rom)
        uow.roms.set_fs_size_bytes(1, None)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.fs_size_bytes is None

    def test_rides_the_upsert_and_refreshes_on_resync(self, uow: SqliteUnitOfWork):
        # Unlike the pin columns, fs_size_bytes rides the sync UPSERT: a re-sync
        # with a new server size OVERWRITES the prior value.
        first = _rom(1)
        first.fs_size_bytes = 1_000_000
        uow.roms.save(first)

        resaved = _rom(1)
        resaved.fs_size_bytes = 2_000_000
        uow.roms.save(resaved)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.fs_size_bytes == 2_000_000


class TestEmulatorOverride:
    def test_round_trips_via_get(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.set_emulator_override(1, "PCSX ReARMed")

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.emulator_override == "PCSX ReARMed"

    def test_defaults_to_none_when_never_pinned(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.emulator_override is None

    def test_setting_none_writes_sql_null(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.set_emulator_override(1, "PCSX ReARMed")
        uow.roms.set_emulator_override(1, None)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.emulator_override is None
        # The column is SQL NULL, not an empty string.
        assert uow._conn is not None
        stored = uow._conn.execute("SELECT emulator_override FROM roms WHERE rom_id = 1").fetchone()[0]
        assert stored is None

    def test_get_all_overrides_omits_null_rows(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        uow.roms.save(_rom(3))
        uow.roms.set_emulator_override(1, "PCSX ReARMed")
        uow.roms.set_emulator_override(3, "Beetle PSX HW")

        overrides = uow.roms.get_all_emulator_overrides()
        assert overrides == {1: "PCSX ReARMed", 3: "Beetle PSX HW"}

    def test_get_all_overrides_empty_when_none_pinned(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        assert uow.roms.get_all_emulator_overrides() == {}


class TestResyncPreservesOverride:
    """A re-sync builds a fresh ``Rom`` with ``emulator_override=None``; the sync
    UPSERT must NOT wipe a pin the user set via ``set_emulator_override`` (Q1)."""

    def test_pin_survives_resync_and_identity_still_updates(self, uow: SqliteUnitOfWork):
        rom_id = 1
        uow.roms.save(_rom(rom_id, app_id=100))
        uow.roms.set_emulator_override(rom_id, "PCSX ReARMed")

        # A normal library re-sync: fresh Rom, no override, changed identity.
        resynced = _rom(rom_id, app_id=200)
        resynced.name = "Renamed Game"
        assert resynced.emulator_override is None
        uow.roms.save(resynced)

        loaded = uow.roms.get(rom_id)
        assert loaded is not None
        # (a) The pin survives the re-sync.
        assert loaded.emulator_override == "PCSX ReARMed"
        # (b) Identity columns still update on that save.
        assert loaded.shortcut_app_id == 200
        assert loaded.name == "Renamed Game"


class TestSelectedDisc:
    def test_round_trips_via_get(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.set_selected_disc(1, "FF7 (Disc 2).cue")

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.selected_disc == "FF7 (Disc 2).cue"

    def test_defaults_to_none_when_never_pinned(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.selected_disc is None

    def test_setting_none_writes_sql_null(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.set_selected_disc(1, "FF7 (Disc 2).cue")
        uow.roms.set_selected_disc(1, None)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.selected_disc is None
        # The column is SQL NULL, not an empty string.
        assert uow._conn is not None
        stored = uow._conn.execute("SELECT selected_disc FROM roms WHERE rom_id = 1").fetchone()[0]
        assert stored is None


class TestResyncPreservesSelectedDisc:
    """A re-sync builds a fresh ``Rom`` with ``selected_disc=None``; the sync
    UPSERT must NOT wipe a disc the user pinned via ``set_selected_disc``."""

    def test_pin_survives_resync_and_identity_still_updates(self, uow: SqliteUnitOfWork):
        rom_id = 1
        uow.roms.save(_rom(rom_id, app_id=100))
        uow.roms.set_selected_disc(rom_id, "FF7 (Disc 2).cue")

        # A normal library re-sync: fresh Rom, no selection, changed identity.
        resynced = _rom(rom_id, app_id=200)
        resynced.name = "Renamed Game"
        assert resynced.selected_disc is None
        uow.roms.save(resynced)

        loaded = uow.roms.get(rom_id)
        assert loaded is not None
        # (a) The pin survives the re-sync.
        assert loaded.selected_disc == "FF7 (Disc 2).cue"
        # (b) Identity columns still update on that save.
        assert loaded.shortcut_app_id == 200
        assert loaded.name == "Renamed Game"

    def test_resync_preserves_both_deviations_together(self, uow: SqliteUnitOfWork):
        """Both per-game deviations survive a re-sync independently."""
        rom_id = 1
        uow.roms.save(_rom(rom_id, app_id=100))
        uow.roms.set_emulator_override(rom_id, "Beetle PSX HW")
        uow.roms.set_selected_disc(rom_id, "FF7 (Disc 3).cue")

        uow.roms.save(_rom(rom_id, app_id=200))

        loaded = uow.roms.get(rom_id)
        assert loaded is not None
        assert loaded.emulator_override == "Beetle PSX HW"
        assert loaded.selected_disc == "FF7 (Disc 3).cue"


class TestAppliedLaunchOptions:
    """The recorded applied launch command (#1383) — read-back, SQL-NULL, and the
    sync-UPSERT-preserves contract, mirroring the two pin columns above."""

    def test_round_trips_via_get(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.set_applied_launch_options(1, "flatpak run net.retrodeck.retrodeck /game.z64")

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.applied_launch_options == "flatpak run net.retrodeck.retrodeck /game.z64"

    def test_defaults_to_none_when_never_recorded(self, uow: SqliteUnitOfWork):
        # A fresh row reads NULL (unknown) — the "never skip on unknown state" contract.
        uow.roms.save(_rom(1))
        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.applied_launch_options is None

    def test_empty_string_is_distinct_from_null(self, uow: SqliteUnitOfWork):
        # "" (recorded uninstalled placeholder) is a real value, not NULL.
        uow.roms.save(_rom(1))
        uow.roms.set_applied_launch_options(1, "")

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.applied_launch_options == ""
        assert uow._conn is not None
        stored = uow._conn.execute("SELECT applied_launch_options FROM roms WHERE rom_id = 1").fetchone()[0]
        assert stored == ""

    def test_survives_resync_and_identity_still_updates(self, uow: SqliteUnitOfWork):
        # The sync UPSERT builds a fresh Rom with applied_launch_options=None; save()
        # must NOT wipe the recorded value, or the delta apply would re-touch a
        # correct shortcut every sync.
        rom_id = 1
        uow.roms.save(_rom(rom_id, app_id=100))
        uow.roms.set_applied_launch_options(rom_id, "flatpak run … /game.z64")

        resynced = _rom(rom_id, app_id=200)
        resynced.name = "Renamed Game"
        assert resynced.applied_launch_options is None
        uow.roms.save(resynced)

        loaded = uow.roms.get(rom_id)
        assert loaded is not None
        assert loaded.applied_launch_options == "flatpak run … /game.z64"
        assert loaded.shortcut_app_id == 200
        assert loaded.name == "Renamed Game"


def _seed_children(uow: SqliteUnitOfWork, rom_id: int) -> None:
    """Seed a row in all five ``ON DELETE CASCADE`` children of ``roms``.

    ``rom_save_sync_states`` is a two-table aggregate, so the ``RomSaveSyncState`` with a
    tracked file also seeds a ``rom_save_files`` row.
    """
    uow.rom_installs.save(
        RomInstall(
            rom_id=rom_id,
            file_path=f"/roms/snes/game_{rom_id}.sfc",
            rom_dir=None,
            platform_slug="snes",
            system="snes",
            installed_at="2026-01-01T00:00:00Z",
        )
    )
    uow.rom_metadata.save(
        rom_id,
        RomMetadata.cached(
            summary="A game",
            genres=("RPG",),
            companies=("Nintendo",),
            first_release_date=None,
            average_rating=None,
            game_modes=("single",),
            player_count="1",
            cached_at=0.0,
            steam_categories=(),
        ),
    )
    playtime = Playtime(total_seconds=3600, session_count=2)
    playtime.enqueue_session(
        device_id="device-1",
        start_time="2026-01-01T10:00:00Z",
        end_time="2026-01-01T11:00:00Z",
        duration_ms=3_600_000,
    )
    uow.playtime.save(rom_id, playtime)
    state = RomSaveSyncState(system="snes")
    state.adopt_baseline(
        "battery.srm",
        tracked_save_id=99,
        last_sync_hash="abc123",
    )
    uow.rom_save_sync_states.save(rom_id, state)


class TestReSaveDoesNotCascade:
    """A re-save (UPSERT) of an existing ROM must update in place, never
    delete-then-insert the parent row — that DELETE would fire ON DELETE CASCADE
    and silently wipe the per-ROM children (#887)."""

    def test_children_survive_a_resave(self, uow: SqliteUnitOfWork):
        rom_id = 1
        uow.roms.save(_rom(rom_id, app_id=100))
        _seed_children(uow, rom_id)

        # Re-save the same ROM with changed columns (a normal library re-sync).
        updated = _rom(rom_id, app_id=200)
        updated.name = "Renamed Game"
        uow.roms.save(updated)

        # (a) The parent row reflects the update.
        loaded = uow.roms.get(rom_id)
        assert loaded is not None
        assert loaded.shortcut_app_id == 200
        assert loaded.name == "Renamed Game"

        # (b) Every cascade child still exists.
        assert uow.rom_installs.get(rom_id) is not None
        assert uow.rom_metadata.get(rom_id) is not None
        playtime = uow.playtime.get(rom_id)
        assert playtime is not None
        assert "2026-01-01T10:00:00Z" in playtime.pending_sessions
        save_state = uow.rom_save_sync_states.get(rom_id)
        assert save_state is not None
        assert "battery.srm" in save_state.files
        assert uow._conn is not None
        file_count = uow._conn.execute(
            "SELECT COUNT(*) FROM rom_save_files WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()[0]
        assert file_count == 1

    def test_genuine_delete_still_cascades(self, uow: SqliteUnitOfWork):
        rom_id = 1
        uow.roms.save(_rom(rom_id))
        _seed_children(uow, rom_id)

        uow.roms.delete(rom_id)

        assert uow.roms.get(rom_id) is None
        assert uow.rom_installs.get(rom_id) is None
        assert uow.rom_metadata.get(rom_id) is None
        assert uow.playtime.get(rom_id) is None
        assert uow.rom_save_sync_states.get(rom_id) is None
        assert uow._conn is not None
        file_count = uow._conn.execute(
            "SELECT COUNT(*) FROM rom_save_files WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()[0]
        assert file_count == 0
        session_count = uow._conn.execute(
            "SELECT COUNT(*) FROM rom_playtime_sessions WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()[0]
        assert session_count == 0
