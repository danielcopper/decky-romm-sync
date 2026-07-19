"""Tests for RommApiAdapter — the consolidated RomM API adapter (>= 4.7.0)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from adapters.romm.romm_api import _TOKEN_SCOPES, RommApiAdapter
from lib.errors import (
    PairingCodeInvalidError,
    PairingCodeOwnerDisabledError,
    PairingCodeRateLimitedError,
    PairingCodeTokenGoneError,
    RommApiError,
    RommForbiddenError,
    RommNotFoundError,
    RommServerError,
    RommSyncDisabledError,
    RommUnprocessableEntityError,
)

if TYPE_CHECKING:
    from models.play_sessions import PlaySessionIngestEntry
    from models.sync import ClientSaveState


def _make_api():
    client = MagicMock()
    client.request = MagicMock()
    client.request_once = MagicMock()
    client.download = MagicMock()
    client.post_json = MagicMock()
    client.put_json = MagicMock()
    client.upload_multipart = MagicMock()
    client.basic_auth_request = MagicMock()
    client.unauthenticated_post_json = MagicMock()
    return RommApiAdapter(client), client


class TestHeartbeat:
    def test_calls_heartbeat_endpoint(self):
        api, client = _make_api()
        client.request.return_value = {"SYSTEM": {"VERSION": "4.7.0"}}
        result = api.heartbeat()
        client.request.assert_called_once_with("/api/heartbeat")
        assert result["SYSTEM"]["VERSION"] == "4.7.0"


class TestHeartbeatOnce:
    def test_uses_single_attempt_short_timeout_request(self):
        """The reachability probe drives the single-attempt ``request_once`` with a
        short timeout — NOT the retrying ``request`` used by ``heartbeat``."""
        api, client = _make_api()
        client.request_once.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        result = api.heartbeat_once()
        client.request_once.assert_called_once_with("/api/heartbeat", timeout=3)
        client.request.assert_not_called()
        assert result["SYSTEM"]["VERSION"] == "4.8.1"


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
        client.request.assert_called_once_with(
            "/api/roms?platform_ids=5&limit=25&offset=10&with_char_index=false&with_filter_values=false"
        )

    def test_default_page_size_and_disabled_aggregations(self):
        """No explicit limit → the shared 500 page size, with the two unused server-side
        aggregations (char index + filter values) turned off."""
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms(5)
        client.request.assert_called_once_with(
            "/api/roms?platform_ids=5&limit=500&offset=0&with_char_index=false&with_filter_values=false"
        )


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

    def test_post_with_autocleanup_limit(self):
        """POST sends autocleanup=true AND the limit so RomM caps retained versions."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", autocleanup_limit=25)
        path = client.upload_multipart.call_args[0][0]
        # autocleanup defaults OFF on RomM, so both flags are required.
        assert "autocleanup=true" in path
        assert "autocleanup_limit=25" in path

    def test_post_without_autocleanup_limit_unchanged(self):
        """POST with no autocleanup_limit (default) keeps the query free of autocleanup."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 1}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba")
        path = client.upload_multipart.call_args[0][0]
        assert "autocleanup" not in path

    def test_put_ignores_autocleanup_limit(self):
        """PUT updates in place and never stacks, so the cap is POST-only — not on PUT."""
        api, client = _make_api()
        client.upload_multipart.return_value = {"id": 5}
        api.upload_save(42, "/tmp/save.srm", "retroarch-mgba", save_id=5, autocleanup_limit=25)
        path = client.upload_multipart.call_args[0][0]
        assert path.startswith("/api/saves/5?")
        assert "autocleanup" not in path

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

    def test_includes_hostname_when_passed(self):
        api, client = _make_api()
        client.post_json.return_value = {"id": "abc-123"}
        api.register_device(
            "steamdeck",
            "linux",
            "decky-romm-sync",
            "0.13.0",
            hostname="machine-abc-123",
        )
        _path, payload = client.post_json.call_args[0]
        assert payload["hostname"] == "machine-abc-123"

    def test_omits_hostname_when_none(self):
        api, client = _make_api()
        client.post_json.return_value = {"id": "abc-123"}
        api.register_device("steamdeck", "linux", "decky-romm-sync", "0.13.0", hostname=None)
        _path, payload = client.post_json.call_args[0]
        assert "hostname" not in payload


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
        # The count probe keeps its limit=1 and rides the same aggregation-off flags.
        assert "limit=1" in url
        assert "with_char_index=false" in url
        assert "with_filter_values=false" in url

    def test_includes_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_updated_after(3, "2024-01-01", limit=10, offset=5)
        url = client.request.call_args[0][0]
        assert "limit=10" in url
        assert "offset=5" in url
        assert "with_char_index=false" in url
        assert "with_filter_values=false" in url


class TestListCollectionRomsUpdatedAfter:
    def test_user_collection_uses_collection_id_param(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_collection_roms_updated_after(7, "user", "2024-01-15T10:30:00+00:00")
        url = client.request.call_args[0][0]
        assert "collection_id=7" in url
        assert "smart_collection_id" not in url
        # Colons and plus sign in the timestamp are encoded.
        assert "updated_after=2024-01-15T10%3A30%3A00%2B00%3A00" in url
        # The count probe keeps its limit=1 and the aggregation-off flags.
        assert "limit=1" in url
        assert "with_char_index=false" in url
        assert "with_filter_values=false" in url

    def test_smart_collection_uses_smart_collection_id_param(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_collection_roms_updated_after(9, "smart", "2024-01-01")
        url = client.request.call_args[0][0]
        assert "smart_collection_id=9" in url
        # ``collection_id`` must not appear as a bare param (it is a substring of
        # ``smart_collection_id``, so anchor the check on the leading ``?``/``&``).
        assert "?collection_id=" not in url and "&collection_id=" not in url

    def test_includes_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_collection_roms_updated_after(7, "user", "2024-01-01", limit=10, offset=5)
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
            resume=False,
            on_meta=None,
        )

    def test_forwards_resume_and_on_meta(self):
        api, client = _make_api()
        cb = lambda current, total: None  # noqa: E731
        meta = lambda supported: None  # noqa: E731
        api.download_rom_content(42, "game.zip", "/tmp/game.zip", progress_callback=cb, resume=True, on_meta=meta)
        client.download.assert_called_once_with(
            "/api/roms/42/content/game.zip",
            "/tmp/game.zip",
            cb,
            resume=True,
            on_meta=meta,
        )


class TestDownloadCover:
    def test_delegates_to_client_download_conditional(self):
        api, client = _make_api()
        client.download_conditional = MagicMock()
        api.download_cover("/assets/covers/zelda.jpg", "/tmp/cover.jpg")
        # Routes through the conditional GET so a validator can revalidate (#1454).
        client.download_conditional.assert_called_once_with(
            "/assets/covers/zelda.jpg", "/tmp/cover.jpg", etag=None, last_modified=None
        )

    def test_forwards_validators_for_conditional_request(self):
        api, client = _make_api()
        client.download_conditional = MagicMock()
        api.download_cover("/c.png", "/tmp/c.png", etag='"abc"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
        client.download_conditional.assert_called_once_with(
            "/c.png", "/tmp/c.png", etag='"abc"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT"
        )


class TestDownloadCoverFromUrl:
    def test_delegates_to_client_download_external(self):
        api, client = _make_api()
        client.download_external = MagicMock()
        api.download_cover_from_url("https://cdn.example.com/x.png", "/tmp/cover.png")
        # The external CDN fetch routes through the bearer-free download_external.
        client.download_external.assert_called_once_with("https://cdn.example.com/x.png", "/tmp/cover.png")


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
        client.request.assert_called_once_with(
            "/api/roms?collection_id=7&limit=25&offset=10&with_char_index=false&with_filter_values=false"
        )


class TestListRomsByVirtualCollection:
    def test_url_encodes_virtual_id(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_virtual_collection("Genre/Action RPG")
        url = client.request.call_args[0][0]
        assert "virtual_collection_id=Genre%2FAction%20RPG" in url
        assert "limit=500" in url  # shared default page size
        assert "with_char_index=false" in url
        assert "with_filter_values=false" in url

    def test_includes_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_virtual_collection("favs", limit=10, offset=5)
        url = client.request.call_args[0][0]
        assert "limit=10" in url
        assert "offset=5" in url
        assert "with_char_index=false" in url
        assert "with_filter_values=false" in url


class TestListSmartCollections:
    def test_returns_list(self):
        api, client = _make_api()
        client.request.return_value = [{"id": 1, "name": "Recent"}, {"id": 2, "name": "Played"}]
        result = api.list_smart_collections()
        client.request.assert_called_once_with("/api/collections/smart")
        assert result == [{"id": 1, "name": "Recent"}, {"id": 2, "name": "Played"}]

    def test_non_list_returns_empty(self):
        api, client = _make_api()
        client.request.return_value = {"error": "bad"}
        assert api.list_smart_collections() == []

    def test_empty_list(self):
        api, client = _make_api()
        client.request.return_value = []
        assert api.list_smart_collections() == []

    def test_propagates_http_error(self):
        from lib.errors import RommServerError

        api, client = _make_api()
        client.request.side_effect = RommServerError(
            "HTTP 500", status_code=500, url="/api/collections/smart", method="GET"
        )
        with pytest.raises(RommServerError):
            api.list_smart_collections()


class TestListRomsBySmartCollection:
    def test_includes_smart_collection_id_and_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_smart_collection(7, limit=25, offset=10)
        client.request.assert_called_once_with(
            "/api/roms?smart_collection_id=7&limit=25&offset=10&with_char_index=false&with_filter_values=false"
        )

    def test_default_pagination(self):
        api, client = _make_api()
        client.request.return_value = {"items": [], "total": 0}
        api.list_roms_by_smart_collection(1)
        client.request.assert_called_once_with(
            "/api/roms?smart_collection_id=1&limit=500&offset=0&with_char_index=false&with_filter_values=false"
        )

    def test_propagates_http_error(self):
        from lib.errors import RommServerError

        api, client = _make_api()
        client.request.side_effect = RommServerError(
            "HTTP 503",
            status_code=503,
            url="/api/roms?smart_collection_id=1",
            method="GET",
        )
        with pytest.raises(RommServerError):
            api.list_roms_by_smart_collection(1)


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


_EMPTY_NEGOTIATE = {
    "session_id": 1,
    "operations": [],
    "total_upload": 0,
    "total_download": 0,
    "total_conflict": 0,
    "total_no_op": 0,
}


class TestNegotiateSync:
    def test_posts_device_id_and_inventory(self):
        api, client = _make_api()
        client.post_json.return_value = {**_EMPTY_NEGOTIATE, "session_id": 7}
        saves: list[ClientSaveState] = [
            {
                "rom_id": 1,
                "file_name": "a.srm",
                "updated_at": "2026-06-01T00:00:00Z",
                "file_size_bytes": 12,
                "slot": "default",
                "content_hash": "abc",
            }
        ]
        result = api.negotiate_sync("dev-1", saves)
        client.post_json.assert_called_once_with("/api/sync/negotiate", {"device_id": "dev-1", "saves": saves})
        assert result["session_id"] == 7

    def test_returns_operations(self):
        api, client = _make_api()
        client.post_json.return_value = {
            **_EMPTY_NEGOTIATE,
            "operations": [{"action": "upload", "rom_id": 1, "file_name": "a.srm", "reason": "newer locally"}],
            "total_upload": 1,
        }
        result = api.negotiate_sync("dev-1", [])
        assert result["operations"][0]["action"] == "upload"

    def test_propagates_http_error(self):
        api, client = _make_api()
        client.post_json.side_effect = RommServerError("boom", status_code=500)
        with pytest.raises(RommServerError):
            api.negotiate_sync("dev-1", [])

    def test_translates_sync_disabled_400(self):
        """A 400 carrying the exact per-device sync-disabled detail becomes RommSyncDisabledError (#1489)."""
        api, client = _make_api()
        err = RommApiError("HTTP 400: Bad Request", url="/api/sync/negotiate", method="POST")
        err.detail = "Sync is disabled for this device"
        client.post_json.side_effect = err
        with pytest.raises(RommSyncDisabledError) as exc_info:
            api.negotiate_sync("dev-1", [])
        # Chains the original so the cause is preserved for logs.
        assert exc_info.value.__cause__ is err

    def test_generic_400_with_other_detail_stays_plain(self):
        """A 400 whose detail is NOT the sync-disabled copy propagates unchanged (#1489)."""
        api, client = _make_api()
        err = RommApiError("HTTP 400: Bad Request", url="/api/sync/negotiate", method="POST")
        err.detail = "Some other validation failure"
        client.post_json.side_effect = err
        with pytest.raises(RommApiError) as exc_info:
            api.negotiate_sync("dev-1", [])
        assert not isinstance(exc_info.value, RommSyncDisabledError)

    def test_400_without_detail_stays_plain(self):
        """A 400 with no detail at all propagates unchanged (#1489)."""
        api, client = _make_api()
        client.post_json.side_effect = RommApiError("HTTP 400: Bad Request", url="/api/sync/negotiate", method="POST")
        with pytest.raises(RommApiError) as exc_info:
            api.negotiate_sync("dev-1", [])
        assert not isinstance(exc_info.value, RommSyncDisabledError)


class TestCompleteSyncSession:
    def test_posts_operation_counts(self):
        api, client = _make_api()
        client.post_json.return_value = {"session": {}}
        api.complete_sync_session(7, operations_completed=3, operations_failed=1)
        client.post_json.assert_called_once_with(
            "/api/sync/sessions/7/complete",
            {"operations_completed": 3, "operations_failed": 1},
        )

    def test_defaults_to_zero_counts(self):
        api, client = _make_api()
        client.post_json.return_value = {"session": {}}
        api.complete_sync_session(7)
        payload = client.post_json.call_args[0][1]
        assert payload == {"operations_completed": 0, "operations_failed": 0}


class TestIngestPlaySessions:
    def test_posts_device_id_and_sessions(self):
        api, client = _make_api()
        client.post_json.return_value = {"results": [], "created_count": 0, "skipped_count": 0}
        sessions: list[PlaySessionIngestEntry] = [
            {"rom_id": 1, "start_time": "t0", "end_time": "t1", "duration_ms": 100}
        ]
        result = api.ingest_play_sessions("dev-1", sessions)
        client.post_json.assert_called_once_with(
            "/api/play-sessions",
            {"device_id": "dev-1", "sessions": sessions},
        )
        assert result["created_count"] == 0

    def test_propagates_http_error(self):
        api, client = _make_api()
        client.post_json.side_effect = RommServerError("boom", status_code=500)
        with pytest.raises(RommServerError):
            api.ingest_play_sessions("dev-1", [])

    def test_propagates_422_with_parsed_detail_intact(self):
        """A whole-request 422 surfaces to the caller as ``RommUnprocessableEntityError`` with its detail."""
        api, client = _make_api()
        detail = [{"loc": ["body", "sessions", 0], "msg": "end_time must be after start_time"}]
        client.post_json.side_effect = RommUnprocessableEntityError("HTTP 422", detail=detail)
        with pytest.raises(RommUnprocessableEntityError) as excinfo:
            api.ingest_play_sessions("dev-1", [{"rom_id": 1, "start_time": "t", "end_time": "t", "duration_ms": 0}])
        assert excinfo.value.detail == detail


class TestListPlaySessions:
    def test_single_short_page_stops_after_one_request(self):
        api, client = _make_api()
        # A page shorter than the limit is the last page — no second request.
        client.request.return_value = [{"id": 1, "rom_id": 42, "duration_ms": 100}]
        result = api.list_play_sessions(42)
        client.request.assert_called_once_with("/api/play-sessions?rom_id=42&limit=100&offset=0")
        assert result[0]["duration_ms"] == 100

    def test_custom_limit_is_passed_with_offset(self):
        api, client = _make_api()
        client.request.return_value = []
        api.list_play_sessions(42, limit=5)
        client.request.assert_called_once_with("/api/play-sessions?rom_id=42&limit=5&offset=0")

    def test_paginates_and_accumulates_across_full_pages(self):
        """A full page (== limit) triggers the next offset; a short page ends the scan (FIX 2)."""
        api, client = _make_api()
        page1 = [{"id": i, "duration_ms": 1000} for i in range(3)]  # full (== limit 3)
        page2 = [{"id": 100, "duration_ms": 500}]  # short → last page
        client.request.side_effect = [page1, page2]
        result = api.list_play_sessions(42, limit=3)
        assert client.request.call_count == 2
        assert client.request.call_args_list[0].args[0] == "/api/play-sessions?rom_id=42&limit=3&offset=0"
        assert client.request.call_args_list[1].args[0] == "/api/play-sessions?rom_id=42&limit=3&offset=3"
        # All 4 rows across both pages are returned (>limit fully summed by the caller).
        assert [s["id"] for s in result] == [0, 1, 2, 100]

    def test_empty_first_page_stops_immediately(self):
        api, client = _make_api()
        client.request.return_value = []
        result = api.list_play_sessions(42, limit=3)
        assert result == []
        client.request.assert_called_once_with("/api/play-sessions?rom_id=42&limit=3&offset=0")

    def test_unwraps_paginated_items_envelope(self):
        api, client = _make_api()
        client.request.return_value = {"items": [{"id": 1, "duration_ms": 50}], "total": 1}
        result = api.list_play_sessions(42)
        assert result == [{"id": 1, "duration_ms": 50}]

    def test_items_envelope_paginates_too(self):
        api, client = _make_api()
        full = {"items": [{"id": i} for i in range(2)], "total": 3}  # full (== limit 2)
        short = {"items": [{"id": 9}], "total": 3}  # short → last
        client.request.side_effect = [full, short]
        result = api.list_play_sessions(42, limit=2)
        assert [s["id"] for s in result] == [0, 1, 9]

    def test_unrecognized_envelope_returns_empty_and_logs(self, caplog):
        """A dict with no ``items`` key ends the scan and leaves a debug breadcrumb (FIX 5)."""
        api, client = _make_api()
        client.request.return_value = {"unexpected": True}
        with caplog.at_level("DEBUG", logger="adapters.romm.romm_api"):
            assert api.list_play_sessions(42) == []
        assert any("unrecognized response envelope" in r.message for r in caplog.records)

    def test_page_cap_guards_against_infinite_loop(self, caplog):
        """A server that always returns a full page stops at the page cap and logs (FIX 2)."""
        from adapters.romm.romm_api import _MAX_PLAY_SESSION_PAGES

        api, client = _make_api()
        # Every page is exactly ``limit`` rows → never short → the cap is the only stop.
        client.request.return_value = [{"id": 1}, {"id": 2}]
        with caplog.at_level("DEBUG", logger="adapters.romm.romm_api"):
            result = api.list_play_sessions(42, limit=2)
        assert client.request.call_count == _MAX_PLAY_SESSION_PAGES
        assert len(result) == _MAX_PLAY_SESSION_PAGES * 2
        assert any("page cap" in r.message for r in caplog.records)


class TestTokenScopes:
    def test_locked_scope_list(self):
        """The 11 requested scopes are fixed and exclude ``me.write``."""
        assert _TOKEN_SCOPES == [
            "me.read",
            "platforms.read",
            "roms.read",
            "roms.user.read",
            "collections.read",
            "firmware.read",
            "assets.read",
            "devices.read",
            "assets.write",
            "devices.write",
            "roms.user.write",
        ]
        assert "me.write" not in _TOKEN_SCOPES
        # Read + write on per-user ROM data: read for native play-session
        # history (#1219), write for the ingest POST.
        assert "roms.user.read" in _TOKEN_SCOPES
        assert "roms.user.write" in _TOKEN_SCOPES


class TestMintClientToken:
    def test_posts_to_client_tokens_with_scopes_and_never_expiry(self):
        api, client = _make_api()
        client.basic_auth_request.return_value = {"id": 7, "raw_token": "rmm_x"}
        result = api.mint_client_token("alice", "secret", token_name="decky-romm-sync (Deck)")
        client.basic_auth_request.assert_called_once_with(
            "/api/client-tokens",
            "alice",
            "secret",
            method="POST",
            data={
                "name": "decky-romm-sync (Deck)",
                "scopes": _TOKEN_SCOPES,
                "expires_in": "never",
            },
        )
        assert result == {"id": 7, "raw_token": "rmm_x"}


class TestDeleteClientToken:
    def test_deletes_via_basic_auth(self):
        api, client = _make_api()
        api.delete_client_token("alice", "secret", token_id=7)
        client.basic_auth_request.assert_called_once_with(
            "/api/client-tokens/7",
            "alice",
            "secret",
            method="DELETE",
        )

    def test_swallows_not_found(self):
        api, client = _make_api()
        client.basic_auth_request.side_effect = RommNotFoundError("404")
        # Already-gone token is the desired end state — no raise.
        assert api.delete_client_token("alice", "secret", token_id=7) is None

    def test_propagates_other_errors(self):
        api, client = _make_api()
        client.basic_auth_request.side_effect = RommServerError("boom", status_code=500)
        with pytest.raises(RommServerError):
            api.delete_client_token("alice", "secret", token_id=7)


class TestExchangePairingCode:
    def test_posts_code_to_public_exchange_endpoint(self):
        api, client = _make_api()
        client.unauthenticated_post_json.return_value = {"id": 5, "raw_token": "rmm_paired"}
        result = api.exchange_pairing_code("ABCD2345")
        client.unauthenticated_post_json.assert_called_once_with(
            "/api/client-tokens/exchange",
            {"code": "ABCD2345"},
        )
        assert result == {"id": 5, "raw_token": "rmm_paired"}

    def test_404_invalid_code_maps_to_pairing_invalid(self):
        api, client = _make_api()
        client.unauthenticated_post_json.side_effect = RommNotFoundError("404")
        with pytest.raises(PairingCodeInvalidError):
            api.exchange_pairing_code("BADCODE1")

    def test_404_unrelated_detail_maps_to_pairing_invalid(self):
        api, client = _make_api()
        err = RommNotFoundError("404")
        err.detail = "Invalid or expired pairing code"
        client.unauthenticated_post_json.side_effect = err
        with pytest.raises(PairingCodeInvalidError):
            api.exchange_pairing_code("BADCODE1")

    def test_404_token_gone_detail_maps_to_pairing_token_gone(self):
        api, client = _make_api()
        err = RommNotFoundError("404")
        err.detail = "Token no longer exists"
        client.unauthenticated_post_json.side_effect = err
        with pytest.raises(PairingCodeTokenGoneError):
            api.exchange_pairing_code("ABCD2345")

    @pytest.mark.parametrize(
        "detail",
        [
            "token no longer exists",  # all-lowercase
            "TOKEN NO LONGER EXISTS",  # all-uppercase
            "The token no longer exists.",  # embedded + trailing punctuation
        ],
    )
    def test_404_token_gone_detail_case_and_punctuation_insensitive(self, detail):
        api, client = _make_api()
        err = RommNotFoundError("404")
        err.detail = detail
        client.unauthenticated_post_json.side_effect = err
        with pytest.raises(PairingCodeTokenGoneError):
            api.exchange_pairing_code("ABCD2345")

    @pytest.mark.parametrize("detail", [None, 123, {"msg": "token no longer exists"}, ["token no longer exists"]])
    def test_404_non_string_or_absent_detail_maps_to_pairing_invalid(self, detail):
        # A non-string / absent detail must never be coerced into the token-gone
        # branch — it falls back to invalid/expired.
        api, client = _make_api()
        err = RommNotFoundError("404")
        err.detail = detail
        client.unauthenticated_post_json.side_effect = err
        with pytest.raises(PairingCodeInvalidError):
            api.exchange_pairing_code("ABCD2345")

    def test_403_maps_to_owner_disabled(self):
        api, client = _make_api()
        client.unauthenticated_post_json.side_effect = RommForbiddenError("403")
        with pytest.raises(PairingCodeOwnerDisabledError):
            api.exchange_pairing_code("ABCD2345")

    def test_429_maps_to_rate_limited(self):
        api, client = _make_api()
        client.unauthenticated_post_json.side_effect = RommServerError("rate", status_code=429)
        with pytest.raises(PairingCodeRateLimitedError):
            api.exchange_pairing_code("ABCD2345")

    def test_5xx_propagates_as_server_error(self):
        api, client = _make_api()
        client.unauthenticated_post_json.side_effect = RommServerError("boom", status_code=500)
        with pytest.raises(RommServerError):
            api.exchange_pairing_code("ABCD2345")
