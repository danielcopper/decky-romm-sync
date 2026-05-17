"""Tests for models.saves dataclasses."""

from dataclasses import asdict

from models.saves import SaveConflict, SaveSyncSettings


class TestSaveConflict:
    def test_construction(self):
        c = SaveConflict(
            rom_id=42,
            filename="pokemon.srm",
            local_path="/saves/pokemon.srm",
            local_hash="abc123",
            local_mtime="2026-01-01T00:00:00+00:00",
            local_size=1024,
            server_save_id=100,
            server_updated_at="2026-01-02T00:00:00Z",
            server_size=2048,
            created_at="2026-01-03T00:00:00+00:00",
        )
        assert c.rom_id == 42
        assert c.filename == "pokemon.srm"

    def test_none_optional_fields(self):
        c = SaveConflict(
            rom_id=42,
            filename="pokemon.srm",
            local_path=None,
            local_hash=None,
            local_mtime=None,
            local_size=None,
            server_save_id=None,
            server_updated_at="",
            server_size=None,
            created_at="2026-01-03T00:00:00+00:00",
        )
        assert c.local_path is None

    def test_asdict(self):
        c = SaveConflict(
            rom_id=1,
            filename="f.srm",
            local_path=None,
            local_hash=None,
            local_mtime=None,
            local_size=None,
            server_save_id=10,
            server_updated_at="2026-01-01T00:00:00Z",
            server_size=512,
            created_at="2026-01-01T00:00:00Z",
        )
        d = asdict(c)
        assert d["rom_id"] == 1
        assert d["server_save_id"] == 10


class TestSaveSyncSettings:
    def test_construction(self):
        s = SaveSyncSettings(
            save_sync_enabled=True,
            sync_before_launch=True,
            sync_after_exit=True,
        )
        assert s.save_sync_enabled is True

    def test_asdict(self):
        s = SaveSyncSettings(
            save_sync_enabled=False,
            sync_before_launch=False,
            sync_after_exit=False,
        )
        d = asdict(s)
        assert d["save_sync_enabled"] is False
        assert d["sync_before_launch"] is False
        assert d["sync_after_exit"] is False
