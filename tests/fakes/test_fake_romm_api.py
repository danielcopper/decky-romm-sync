"""Smoke tests for ``FakeRommApi`` — fixture sanity and Protocol satisfaction."""

from __future__ import annotations

import pytest

from fakes.fake_romm_api import FakeRommApi
from services.protocols.transport import (
    RommAchievementsApi,
    RommConnectionApi,
    RommDeviceApi,
    RommFirmwareApi,
    RommLibraryApi,
    RommPlatformReader,
    RommPlaytimeApi,
    RommRomReader,
    RommSaveApi,
    RommSyncApi,
    RommTokenApi,
    RommVersion,
)


class TestConstruction:
    """Construction with defaults works and exposes sensible empty seeded state."""

    def test_default_construction_succeeds(self) -> None:
        api = FakeRommApi()
        assert api.platforms == []
        assert api.roms == {}
        assert api.firmware_files == []
        assert api.collections == []
        assert api.virtual_collections == {}
        assert api.devices == []
        assert api.saves == {}
        assert api.notes == {}
        assert api.call_log == []

    def test_call_log_records_method_calls(self) -> None:
        api = FakeRommApi()
        api.heartbeat()
        api.list_platforms()
        assert [name for name, _, _ in api.call_log] == ["heartbeat", "list_platforms"]


class TestFailOnNext:
    """``fail_on_next(exc)`` raises on the next call and clears the arming."""

    def test_raises_on_next_call(self) -> None:
        api = FakeRommApi()
        api.fail_on_next(OSError("boom"))
        with pytest.raises(OSError, match="boom"):
            api.list_platforms()

    def test_is_one_shot(self) -> None:
        api = FakeRommApi()
        api.fail_on_next(RuntimeError("first"))
        with pytest.raises(RuntimeError):
            api.heartbeat()
        # Next call after arming consumed succeeds.
        assert api.heartbeat() == {"status": "ok"}

    def test_arms_any_method(self) -> None:
        api = FakeRommApi()
        api.fail_on_next(ValueError("on next"))
        with pytest.raises(ValueError, match="on next"):
            api.list_firmware()


class TestPerMethodSideEffects:
    """Per-method ``*_side_effect`` attrs raise on every call until cleared."""

    def test_list_firmware_side_effect_fires_every_call(self) -> None:
        api = FakeRommApi()
        api.list_firmware_side_effect = OSError("offline")
        with pytest.raises(OSError, match="offline"):
            api.list_firmware()
        with pytest.raises(OSError, match="offline"):
            api.list_firmware()

    def test_heartbeat_side_effect(self) -> None:
        api = FakeRommApi()
        api.heartbeat_side_effect = ConnectionError("down")
        with pytest.raises(ConnectionError):
            api.heartbeat()

    def test_fail_on_next_takes_precedence_over_side_effect(self) -> None:
        api = FakeRommApi()
        api.list_platforms_side_effect = OSError("persistent")
        api.fail_on_next(RuntimeError("one shot"))
        with pytest.raises(RuntimeError, match="one shot"):
            api.list_platforms()
        # One-shot consumed; persistent side effect still fires.
        with pytest.raises(OSError, match="persistent"):
            api.list_platforms()


class TestSeededReads:
    """Seeded in-memory state flows through reads."""

    def test_list_platforms_returns_seeded(self) -> None:
        api = FakeRommApi()
        api.platforms = [{"id": 1, "slug": "snes"}, {"id": 2, "slug": "psx"}]
        assert api.list_platforms() == [{"id": 1, "slug": "snes"}, {"id": 2, "slug": "psx"}]

    def test_list_roms_filters_by_platform_and_paginates(self) -> None:
        api = FakeRommApi()
        api.roms = {
            1: {"id": 1, "platform_id": 10, "name": "A"},
            2: {"id": 2, "platform_id": 10, "name": "B"},
            3: {"id": 3, "platform_id": 99, "name": "C"},
        }
        result = api.list_roms(platform_id=10, limit=1, offset=1)
        assert result["total"] == 2
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "B"

    def test_get_rom_falls_back_to_id_only_for_unknown(self) -> None:
        api = FakeRommApi()
        assert api.get_rom(42) == {"id": 42}

    def test_get_rom_with_notes_includes_seeded_notes(self) -> None:
        api = FakeRommApi()
        api.roms = {1: {"id": 1, "name": "Tetris"}}
        api.notes = {1: [{"id": 100, "rom_id": 1, "user_id": 7, "note_raw_markdown": "hi"}]}
        detail = api.get_rom_with_notes(1)
        assert detail["name"] == "Tetris"
        assert detail["all_user_notes"][0]["id"] == 100


class TestDownloads:
    """Download methods write payload bytes to ``dest_path`` via pathlib."""

    def test_download_rom_content_writes_staged_payload(self, tmp_path) -> None:
        api = FakeRommApi()
        api.download_payloads["rom:5:game.zip"] = b"hello"
        dest = tmp_path / "game.zip"
        api.download_rom_content(5, "game.zip", str(dest))
        assert dest.read_bytes() == b"hello"

    def test_download_firmware_writes_default_empty_payload(self, tmp_path) -> None:
        api = FakeRommApi()
        dest = tmp_path / "bios" / "fw.bin"
        api.download_firmware(1, "fw.bin", str(dest))
        # Parent dirs auto-created, default empty payload written.
        assert dest.exists()
        assert dest.read_bytes() == b""

    def test_download_save_uses_set_server_save_content(self, tmp_path) -> None:
        api = FakeRommApi()
        api.set_server_save_content(99, b"save-bytes")
        dest = tmp_path / "out.sav"
        api.download_save(99, str(dest))
        assert dest.read_bytes() == b"save-bytes"


class TestUploadSave:
    """``upload_save`` synthesises ids and records uploads."""

    def test_creates_new_save_with_auto_id(self) -> None:
        api = FakeRommApi()
        result = api.upload_save(rom_id=1, file_path="/tmp/save.sav", emulator="snes9x")
        assert result["rom_id"] == 1
        assert result["file_name"] == "save.sav"
        assert result["id"] in api.saves


class TestDeviceRegistration:
    """Device registration synthesises ids and stores in ``devices``."""

    def test_register_device_assigns_id_and_stores(self) -> None:
        api = FakeRommApi()
        device = api.register_device("deck", "steamos", "decky-romm-sync", "0.1.0")
        assert device["id"] == "device-1"
        assert len(api.devices) == 1
        assert api.list_devices()[0]["name"] == "deck"


class TestClientTokens:
    """Mint/delete record calls and honour staged responses."""

    def test_mint_returns_staged_response(self) -> None:
        api = FakeRommApi()
        api.mint_client_token_response = {"id": 9, "raw_token": "rmm_seeded"}
        result = api.mint_client_token("alice", "secret", token_name="decky-romm-sync (Deck)")
        assert result == {"id": 9, "raw_token": "rmm_seeded"}
        name, args, kwargs = api.call_log[-1]
        assert name == "mint_client_token"
        assert args == ("alice", "secret")
        assert kwargs == {"token_name": "decky-romm-sync (Deck)"}

    def test_delete_records_token_id(self) -> None:
        api = FakeRommApi()
        api.delete_client_token("alice", "secret", token_id=42)
        assert api.deleted_token_ids == [42]

    def test_mint_side_effect_fires(self) -> None:
        api = FakeRommApi()
        api.mint_client_token_side_effect = OSError("forbidden")
        with pytest.raises(OSError, match="forbidden"):
            api.mint_client_token("u", "p", token_name="x")


class TestProtocolSatisfaction:
    """``FakeRommApi`` structurally satisfies every RomM Protocol.

    The transport Protocols are not ``@runtime_checkable`` (deliberate —
    they're typing-only contracts), so this test verifies the structural
    surface by checking every Protocol member is present and callable on
    the fake.
    """

    @pytest.mark.parametrize(
        "protocol",
        [
            RommVersion,
            RommPlatformReader,
            RommRomReader,
            RommFirmwareApi,
            RommPlaytimeApi,
            RommDeviceApi,
            RommSaveApi,
            RommAchievementsApi,
            RommConnectionApi,
            RommLibraryApi,
            RommSyncApi,
            RommTokenApi,
        ],
    )
    def test_implements_every_protocol_member(self, protocol) -> None:
        fake = FakeRommApi()
        # Protocol.__protocol_attrs__ is set by ``typing.Protocol`` for
        # any subclass; falls back to scanning non-dunder attrs.
        members = getattr(protocol, "__protocol_attrs__", None) or {
            name for name in dir(protocol) if not name.startswith("_")
        }
        missing = [name for name in members if not callable(getattr(fake, name, None))]
        assert not missing, f"FakeRommApi missing {protocol.__name__} members: {missing}"


class TestFixture:
    """Conftest ``fake_romm_api`` fixture yields a fresh instance per test."""

    def test_fixture_returns_fake_romm_api(self, fake_romm_api) -> None:
        assert isinstance(fake_romm_api, FakeRommApi)
        assert fake_romm_api.platforms == []

    def test_fixture_is_function_scoped(self, fake_romm_api) -> None:
        # Mutate state — the next test re-using the fixture must get a fresh one.
        fake_romm_api.platforms.append({"id": 1})
        assert fake_romm_api.platforms == [{"id": 1}]

    def test_fixture_state_does_not_leak(self, fake_romm_api) -> None:
        # If function scoping works, the previous test's mutation is gone.
        assert fake_romm_api.platforms == []
