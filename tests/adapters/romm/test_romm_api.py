"""Tests for RommApiAdapter — the consolidated RomM API adapter (>= 4.7.0)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from adapters.romm.romm_api import RommApiAdapter


def _make_api():
    client = MagicMock()
    client.request = MagicMock()
    client.download = MagicMock()
    client.post_json = MagicMock()
    client.put_json = MagicMock()
    client.upload_multipart = MagicMock()
    return RommApiAdapter(client), client


class TestHeartbeat:
    def test_calls_heartbeat_endpoint(self):
        api, client = _make_api()
        client.request.return_value = {"SYSTEM": {"VERSION": "4.7.0"}}
        result = api.heartbeat()
        client.request.assert_called_once_with("/api/heartbeat")
        assert result["SYSTEM"]["VERSION"] == "4.7.0"


class TestListPlatforms:
    def test_calls_platforms_endpoint(self):
        api, client = _make_api()
        client.request.return_value = [{"id": 1, "slug": "snes"}]
        result = api.list_platforms()
        client.request.assert_called_once_with("/api/platforms")
        assert result == [{"id": 1, "slug": "snes"}]


class TestGetRom:
    def test_calls_rom_endpoint(self):
        api, client = _make_api()
        client.request.return_value = {"id": 42, "name": "Zelda"}
        result = api.get_rom(42)
        client.request.assert_called_once_with("/api/roms/42")
        assert result["id"] == 42


class TestListRoms:
    def test_includes_platform_id_and_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms(5, limit=25, offset=10)
        client.request.assert_called_once_with("/api/roms?platform_ids=5&limit=25&offset=10")


class TestDownloadSave:
    def test_uses_content_endpoint(self):
        api, client = _make_api()
        api.download_save(99, "/tmp/save.srm")
        client.download.assert_called_once_with("/api/saves/99/content", "/tmp/save.srm")

    def test_no_metadata_round_trip(self):
        """download_save should NOT call request() to fetch metadata first."""
        api, client = _make_api()
        api.download_save(5, "/tmp/save.srm")
        client.request.assert_not_called()


class TestUploadSave:
    def test_post_new_save(self):
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        result = api.upload_save(42, "/tmp/save.srm", "retroarch-mgba")
        client.upload_multipart.assert_called_once_with(
            "/api/saves?rom_id=42&emulator=retroarch-mgba",
            "/tmp/save.srm",
            method="POST",
        )
        assert result == {"id": 1}

    def test_put_with_save_id(self):
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 5}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", save_id=5)
        client.upload_multipart.assert_called_once_with(
            "/api/saves/5?rom_id=42&emulator=retroarch-mgba",
            "/tmp/save.srm",
            method="PUT",
        )

    def test_with_device_id(self):
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", device_id="abc-123")
        path = client.upload_multipart.call_args[0][0]
        assert "device_id=abc-123" in path

    def test_with_slot(self):
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", slot="default")
        path = client.upload_multipart.call_args[0][0]
        assert "slot=default" in path

    def test_url_encodes_slot_with_special_chars(self):
        """Slot with reserved URL characters (&, =, /, ?, +, space) is percent-encoded."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", slot="Mom & Dad=draft+1?/x")
        path = client.upload_multipart.call_args[0][0]
        # raw special chars must NOT appear in the value segment
        assert "slot=Mom%20%26%20Dad%3Ddraft%2B1%3F%2Fx" in path
        assert "slot=Mom & Dad=draft+1?/x" not in path

    def test_url_encodes_slot_empty_string(self):
        """Empty string slot is encoded but still present (caller asked for it)."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", slot="")
        path = client.upload_multipart.call_args[0][0]
        # empty string serializes as "slot=" — important: still on the URL
        assert "slot=" in path

    def test_url_encodes_slot_non_ascii(self):
        """Non-ASCII slot (e.g. Japanese) is percent-encoded as UTF-8."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", slot="スロット")
        path = client.upload_multipart.call_args[0][0]
        # UTF-8 of スロット = E382B9 E383AD E38383 E38388
        assert "slot=%E3%82%B9%E3%83%AD%E3%83%83%E3%83%88" in path
        assert "スロット" not in path

    def test_url_encodes_device_id_with_special_chars(self):
        """device_id is encoded defensively even though it's normally a UUID."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", device_id="abc&xyz=1")
        path = client.upload_multipart.call_args[0][0]
        assert "device_id=abc%26xyz%3D1" in path
        assert "device_id=abc&xyz=1" not in path

    def test_with_overwrite_true(self):
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", overwrite=True)
        path = client.upload_multipart.call_args[0][0]
        assert "overwrite=true" in path

    def test_overwrite_false_not_in_query(self):
        """overwrite=false is the default — don't clutter the query string."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", overwrite=False)
        path = client.upload_multipart.call_args[0][0]
        assert "overwrite" not in path

    def test_encodes_emulator(self):
        """Slash in emulator name is encoded (safe="" — house style)."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retro arch/core")
        path = client.upload_multipart.call_args[0][0]
        assert "emulator=retro%20arch%2Fcore" in path
        assert "retro arch/core" not in path

    def test_409_raises_conflict_error(self):
        """409 from server propagates as RommConflictError."""
        from lib.errors import RommConflictError

        api, client = _make_api()
        client.upload_multipart.side_effect = RommConflictError("HTTP 409: Conflict", url="/api/saves", method="POST")
        with pytest.raises(RommConflictError):
            api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", device_id="abc")


class TestDownloadSaveContent:
    def test_basic_download(self):
        api, client = _make_api()
        api.download_save_content(99, "/tmp/save.srm")
        client.download.assert_called_once_with("/api/saves/99/content", "/tmp/save.srm")

    def test_with_device_id_optimistic_true(self):
        api, client = _make_api()
        api.download_save_content(99, "/tmp/save.srm", device_id="abc-123")
        client.download.assert_called_once_with(
            "/api/saves/99/content?device_id=abc-123&optimistic=true",
            "/tmp/save.srm",
        )

    def test_with_device_id_optimistic_false(self):
        api, client = _make_api()
        api.download_save_content(99, "/tmp/save.srm", device_id="abc-123", optimistic=False)
        client.download.assert_called_once_with(
            "/api/saves/99/content?device_id=abc-123&optimistic=false",
            "/tmp/save.srm",
        )

    def test_without_device_id_no_query_params(self):
        api, client = _make_api()
        api.download_save_content(42, "/tmp/save.srm")
        client.download.assert_called_once_with("/api/saves/42/content", "/tmp/save.srm")

    def test_url_encodes_device_id_ascii_round_trip(self):
        """ASCII device_id (UUID-like) survives encoding unchanged."""
        api, client = _make_api()
        api.download_save_content(99, "/tmp/save.srm", device_id="abc-123")
        url = client.download.call_args[0][0]
        assert "device_id=abc-123" in url

    def test_url_encodes_device_id_with_special_chars(self):
        """device_id with reserved URL characters is percent-encoded."""
        api, client = _make_api()
        api.download_save_content(99, "/tmp/save.srm", device_id="abc&xyz/1")
        url = client.download.call_args[0][0]
        assert "device_id=abc%26xyz%2F1" in url
        assert "device_id=abc&xyz/1" not in url


class TestListCollections:
    def test_returns_list(self):
        api, client = _make_api()
        client.request.return_value = [{"id": 1, "name": "Favorites"}]
        result = api.list_collections()
        client.request.assert_called_once_with("/api/collections")
        assert result == [{"id": 1, "name": "Favorites"}]

    def test_non_list_returns_empty(self):
        api, client = _make_api()
        client.request.return_value = {"error": "bad"}
        assert api.list_collections() == []


class TestRegisterDevice:
    def test_uses_client_version_key(self):
        api, client = _make_api()
        client.post_json.return_value = {
            "id": "abc-123",
            "name": "steamdeck",
            "created_at": "2026-01-01T00:00:00Z",
        }
        result = api.register_device("steamdeck", "linux", "decky-romm-sync", "0.13.0")
        _name, payload = client.post_json.call_args[0]
        assert payload["client_version"] == "0.13.0"
        assert "version" not in payload
        assert result["id"] == "abc-123"

    def test_posts_to_devices_endpoint(self):
        api, client = _make_api()
        client.post_json.return_value = {
            "id": "abc-123",
            "name": "steamdeck",
            "created_at": "2026-01-01T00:00:00Z",
        }
        api.register_device("steamdeck", "linux", "decky-romm-sync", "0.13.0")
        client.post_json.assert_called_once_with(
            "/api/devices",
            {
                "name": "steamdeck",
                "platform": "linux",
                "client": "decky-romm-sync",
                "client_version": "0.13.0",
            },
        )


class TestListDevices:
    def test_returns_array(self):
        api, client = _make_api()
        client.request.return_value = [
            {"id": "abc-123", "name": "steamdeck"},
            {"id": "def-456", "name": "laptop"},
        ]
        result = api.list_devices()
        client.request.assert_called_once_with("/api/devices")
        assert len(result) == 2
        assert result[0]["id"] == "abc-123"

    def test_handles_non_list_response(self):
        api, client = _make_api()
        client.request.return_value = {"error": "unexpected"}
        result = api.list_devices()
        assert result == []

    def test_handles_none_response(self):
        api, client = _make_api()
        client.request.return_value = None
        result = api.list_devices()
        assert result == []


class TestUpdateDevice:
    def test_sends_put_with_filtered_payload(self):
        api, client = _make_api()
        client.put_json.return_value = {"id": "abc-123", "client_version": "0.14.0"}
        result = api.update_device("abc-123", client_version="0.14.0", name=None)
        client.put_json.assert_called_once_with(
            "/api/devices/abc-123",
            {"client_version": "0.14.0"},
        )
        assert result["id"] == "abc-123"

    def test_excludes_none_fields(self):
        api, client = _make_api()
        client.put_json.return_value = {"id": "abc-123"}
        api.update_device("abc-123", name=None, client_version=None, sync_enabled=None)
        _url, payload = client.put_json.call_args[0]
        assert payload == {}

    def test_url_contains_device_id(self):
        api, client = _make_api()
        client.put_json.return_value = {"id": "xyz-999"}
        api.update_device("xyz-999", client_version="1.0.0")
        url = client.put_json.call_args[0][0]
        assert "xyz-999" in url

    def test_url_encodes_device_id_ascii_round_trip(self):
        """ASCII device_id (UUID-like) survives encoding unchanged."""
        api, client = _make_api()
        client.put_json.return_value = {"id": "abc-123"}
        api.update_device("abc-123", client_version="1.0.0")
        url = client.put_json.call_args[0][0]
        assert url == "/api/devices/abc-123"

    def test_url_encodes_device_id_with_special_chars(self):
        """device_id with reserved URL characters is percent-encoded."""
        api, client = _make_api()
        client.put_json.return_value = {"id": "abc&xyz/1"}
        api.update_device("abc&xyz/1", client_version="1.0.0")
        url = client.put_json.call_args[0][0]
        assert url == "/api/devices/abc%26xyz%2F1"
        assert "abc&xyz/1" not in url


class TestSetVersion:
    def test_stores_version(self):
        api, _client = _make_api()
        assert api._version is None
        api.set_version("4.7.0")
        assert api._version == "4.7.0"


class TestGetVersion:
    def test_returns_none_when_unset(self):
        api, _client = _make_api()
        assert api.get_version() is None

    def test_returns_stored_version(self):
        api, _client = _make_api()
        api.set_version("4.8.1")
        assert api.get_version() == "4.8.1"


class TestListSaves:
    def test_base_call(self):
        api, client = _make_api()
        client.request.return_value = [{"id": 1}]
        result = api.list_saves(42)
        client.request.assert_called_once_with("/api/saves?rom_id=42")
        assert result == [{"id": 1}]

    def test_with_device_id(self):
        api, client = _make_api()
        client.request.return_value = [{"id": 1, "device_syncs": []}]
        api.list_saves(42, device_id="abc-123")
        client.request.assert_called_once_with("/api/saves?rom_id=42&device_id=abc-123")

    def test_with_slot(self):
        api, client = _make_api()
        client.request.return_value = []
        api.list_saves(42, slot="default")
        client.request.assert_called_once_with("/api/saves?rom_id=42&slot=default")

    def test_with_device_id_and_slot(self):
        api, client = _make_api()
        client.request.return_value = []
        api.list_saves(42, device_id="abc", slot="default")
        client.request.assert_called_once_with("/api/saves?rom_id=42&device_id=abc&slot=default")

    def test_non_list_returns_empty(self):
        api, client = _make_api()
        client.request.return_value = {"error": "bad"}
        assert api.list_saves(42, device_id="abc") == []

    def test_url_encodes_slot_with_special_chars(self):
        """Slot with reserved URL characters is percent-encoded."""
        api, client = _make_api()
        client.request.return_value = []
        api.list_saves(42, slot="Mom & Dad=draft+1?/x")
        url = client.request.call_args[0][0]
        assert "slot=Mom%20%26%20Dad%3Ddraft%2B1%3F%2Fx" in url
        assert "slot=Mom & Dad=draft+1?/x" not in url

    def test_url_encodes_slot_ascii_safe_round_trips(self):
        """Plain ASCII slot like 'Desktop' is unchanged."""
        api, client = _make_api()
        client.request.return_value = []
        api.list_saves(42, slot="Desktop")
        url = client.request.call_args[0][0]
        assert "slot=Desktop" in url

    def test_url_encodes_slot_non_ascii(self):
        """Non-ASCII slot is percent-encoded as UTF-8."""
        api, client = _make_api()
        client.request.return_value = []
        api.list_saves(42, slot="スロット")
        url = client.request.call_args[0][0]
        assert "slot=%E3%82%B9%E3%83%AD%E3%83%83%E3%83%88" in url

    def test_url_encodes_device_id_with_special_chars(self):
        """device_id is encoded defensively even though it's normally a UUID."""
        api, client = _make_api()
        client.request.return_value = []
        api.list_saves(42, device_id="abc&xyz=1")
        url = client.request.call_args[0][0]
        assert "device_id=abc%26xyz%3D1" in url
        assert "device_id=abc&xyz=1" not in url


class TestGetCurrentUser:
    def test_calls_users_me_endpoint(self):
        api, client = _make_api()
        client.request.return_value = {"id": 1, "username": "admin"}
        result = api.get_current_user()
        client.request.assert_called_once_with("/api/users/me")
        assert result["username"] == "admin"


class TestListRomsUpdatedAfter:
    def test_url_encodes_updated_after(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_updated_after(5, "2024-01-15T10:30:00+00:00")
        url = client.request.call_args[0][0]
        # Colons and plus sign must be encoded
        assert "updated_after=2024-01-15T10%3A30%3A00%2B00%3A00" in url
        assert "platform_ids=5" in url

    def test_includes_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_updated_after(3, "2024-01-01", limit=10, offset=5)
        url = client.request.call_args[0][0]
        assert "limit=10" in url
        assert "offset=5" in url


class TestDownloadRomContent:
    def test_url_encodes_filename(self):
        api, client = _make_api()
        api.download_rom_content(42, "My Game (USA).zip", "/tmp/game.zip")
        url = client.download.call_args[0][0]
        assert url == "/api/roms/42/content/My%20Game%20%28USA%29.zip"

    def test_passes_dest_and_callback(self):
        api, client = _make_api()
        cb = lambda current, total: None  # noqa: E731
        api.download_rom_content(42, "game.zip", "/tmp/game.zip", progress_callback=cb)
        client.download.assert_called_once_with(
            "/api/roms/42/content/game.zip",
            "/tmp/game.zip",
            cb,
        )


class TestDownloadCover:
    def test_delegates_to_client_download(self):
        api, client = _make_api()
        api.download_cover("/assets/covers/zelda.jpg", "/tmp/cover.jpg")
        client.download.assert_called_once_with("/assets/covers/zelda.jpg", "/tmp/cover.jpg")


class TestListVirtualCollections:
    def test_returns_list(self):
        api, client = _make_api()
        client.request.return_value = [{"name": "Favorites"}]
        result = api.list_virtual_collections("favorites")
        client.request.assert_called_once_with("/api/collections/virtual?type=favorites")
        assert result == [{"name": "Favorites"}]

    def test_non_list_returns_empty(self):
        api, client = _make_api()
        client.request.return_value = {"error": "not found"}
        assert api.list_virtual_collections("favorites") == []


class TestListRomsByCollection:
    def test_includes_collection_id_and_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_collection(7, limit=25, offset=10)
        client.request.assert_called_once_with("/api/roms?collection_id=7&limit=25&offset=10")


class TestListRomsByVirtualCollection:
    def test_url_encodes_virtual_id(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_virtual_collection("Genre/Action RPG")
        url = client.request.call_args[0][0]
        assert "virtual_collection_id=Genre%2FAction%20RPG" in url

    def test_includes_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_virtual_collection("favs", limit=10, offset=5)
        url = client.request.call_args[0][0]
        assert "limit=10" in url
        assert "offset=5" in url


class TestListFirmware:
    def test_calls_firmware_endpoint(self):
        api, client = _make_api()
        client.request.return_value = [{"id": 1, "name": "bios.bin"}]
        result = api.list_firmware()
        client.request.assert_called_once_with("/api/firmware")
        assert result == [{"id": 1, "name": "bios.bin"}]


class TestGetFirmware:
    def test_calls_firmware_by_id(self):
        api, client = _make_api()
        client.request.return_value = {"id": 5, "name": "scph1001.bin"}
        result = api.get_firmware(5)
        client.request.assert_called_once_with("/api/firmware/5")
        assert result["id"] == 5


class TestDownloadFirmware:
    def test_url_encodes_filename(self):
        api, client = _make_api()
        api.download_firmware(3, "BIOS (JP).bin", "/tmp/bios.bin")
        url = client.download.call_args[0][0]
        assert url == "/api/firmware/3/content/BIOS%20%28JP%29.bin"
        assert client.download.call_args[0][1] == "/tmp/bios.bin"

    def test_simple_filename(self):
        api, client = _make_api()
        api.download_firmware(3, "scph1001.bin", "/tmp/bios.bin")
        client.download.assert_called_once_with(
            "/api/firmware/3/content/scph1001.bin",
            "/tmp/bios.bin",
        )


class TestConfirmDownload:
    def test_posts_device_id(self):
        api, client = _make_api()
        client.post_json.return_value = {"ok": True}
        result = api.confirm_download(99, "device-abc")
        client.post_json.assert_called_once_with(
            "/api/saves/99/downloaded",
            {"device_id": "device-abc"},
        )
        assert result == {"ok": True}

    def test_propagates_http_error(self):
        """5xx from server propagates as RommServerError."""
        from lib.errors import RommServerError

        api, client = _make_api()
        client.post_json.side_effect = RommServerError(
            "HTTP 500: Server Error",
            status_code=500,
            url="/api/saves/99/downloaded",
            method="POST",
        )
        with pytest.raises(RommServerError):
            api.confirm_download(99, "device-abc")


class TestGetSaveMetadata:
    def test_calls_save_by_id(self):
        api, client = _make_api()
        client.request.return_value = {"id": 42, "file_name": "save.srm"}
        result = api.get_save_metadata(42)
        client.request.assert_called_once_with("/api/saves/42")
        assert result["file_name"] == "save.srm"


class TestGetSaveSummary:
    def test_without_device_id(self):
        api, client = _make_api()
        client.request.return_value = {"total": 3}
        result = api.get_save_summary(42)
        client.request.assert_called_once_with("/api/saves/summary?rom_id=42")
        assert result["total"] == 3

    def test_with_device_id(self):
        api, client = _make_api()
        client.request.return_value = {"total": 1}
        api.get_save_summary(42, device_id="abc-123")
        client.request.assert_called_once_with("/api/saves/summary?rom_id=42&device_id=abc-123")

    def test_url_encodes_device_id_with_special_chars(self):
        """device_id is encoded defensively even though it's normally a UUID."""
        api, client = _make_api()
        client.request.return_value = {"total": 0}
        api.get_save_summary(42, device_id="abc&xyz=1")
        url = client.request.call_args[0][0]
        assert "device_id=abc%26xyz%3D1" in url
        assert "device_id=abc&xyz=1" not in url


class TestDeleteServerSaves:
    def test_posts_save_ids(self):
        api, client = _make_api()
        client.post_json.return_value = {"deleted": 2}
        result = api.delete_server_saves([10, 20])
        client.post_json.assert_called_once_with("/api/saves/delete", {"saves": [10, 20]})
        assert result["deleted"] == 2


class TestGetRomWithNotes:
    def test_calls_rom_endpoint(self):
        api, client = _make_api()
        client.request.return_value = {"id": 42, "all_user_notes": []}
        result = api.get_rom_with_notes(42)
        client.request.assert_called_once_with("/api/roms/42")
        assert result["all_user_notes"] == []


class TestCreateNote:
    def test_posts_to_notes_endpoint(self):
        api, client = _make_api()
        data = {"raw_markdown": "played 30 min"}
        client.post_json.return_value = {"id": 1}
        result = api.create_note(42, data)
        client.post_json.assert_called_once_with("/api/roms/42/notes", data)
        assert result["id"] == 1


class TestUpdateNote:
    def test_puts_to_note_endpoint(self):
        api, client = _make_api()
        data = {"raw_markdown": "updated"}
        client.put_json.return_value = {"id": 7}
        result = api.update_note(42, 7, data)
        client.put_json.assert_called_once_with("/api/roms/42/notes/7", data)
        assert result["id"] == 7
