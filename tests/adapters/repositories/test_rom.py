"""Tests for ``SqliteRomRepository`` over the ``roms`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.playtime import Playtime
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_metadata import RomMetadata
from domain.rom_save_state import RomSaveState

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
        assert uow.rom_save_states.get(1) is not None


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


class TestUpsert:
    def test_save_existing_id_overwrites(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, app_id=100))
        uow.roms.save(_rom(1, app_id=200))

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.shortcut_app_id == 200
        assert uow.roms.count() == 1


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


def _seed_children(uow: SqliteUnitOfWork, rom_id: int) -> None:
    """Seed a row in all five ``ON DELETE CASCADE`` children of ``roms``.

    ``rom_save_states`` is a two-table aggregate, so the ``RomSaveState`` with a
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
    uow.playtime.save(rom_id, Playtime(total_seconds=3600, session_count=2))
    state = RomSaveState(system="snes")
    state.adopt_baseline(
        "battery.srm",
        tracked_save_id=99,
        last_sync_hash="abc123",
    )
    uow.rom_save_states.save(rom_id, state)


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
        assert uow.playtime.get(rom_id) is not None
        save_state = uow.rom_save_states.get(rom_id)
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
        assert uow.rom_save_states.get(rom_id) is None
        assert uow._conn is not None
        file_count = uow._conn.execute(
            "SELECT COUNT(*) FROM rom_save_files WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()[0]
        assert file_count == 0
