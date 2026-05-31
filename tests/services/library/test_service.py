"""Façade integration tests for LibraryService — public callable surface end-to-end."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fakes.fake_settings_persister import FakeSettingsPersister

from domain.rom import Rom
from domain.sync_diff import classify_roms

# conftest.py patches decky before this import
from tests.services.library._helpers import (
    _make_loop_raising,
    _make_loop_with_executor,
    _make_registry_entry,
    rebind_loop,
)


def _seed_rom(uow, rom_id, *, app_id, platform_slug, name="Game"):
    """Insert a bound (or unbound when app_id is None) ROM into the shared fake UoW."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=f"{name}.zip",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(rom)


class TestGetPlatforms:
    """Tests for get_platforms() — lines 90-117."""

    @pytest.mark.asyncio
    async def test_returns_platforms_with_rom_count(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 10},
                {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
            ]
        )
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is True
        assert len(result["platforms"]) == 2
        assert result["platforms"][0]["name"] == "N64"
        assert result["platforms"][0]["rom_count"] == 10
        assert result["platforms"][1]["name"] == "SNES"

    @pytest.mark.asyncio
    async def test_skips_zero_rom_count(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 10},
                {"id": 2, "name": "Empty", "slug": "empty", "rom_count": 0},
            ]
        )
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is True
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["name"] == "N64"

    @pytest.mark.asyncio
    async def test_sync_enabled_from_settings(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 10},
                {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
            ]
        )
        rebind_loop(plugin._sync_service, mock_loop)
        plugin.settings["enabled_platforms"] = {"1": True, "2": False}

        result = await plugin._sync_service.get_platforms()
        assert result["platforms"][0]["sync_enabled"] is True
        assert result["platforms"][1]["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_default_sync_enabled_when_no_prefs(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}])
        rebind_loop(plugin._sync_service, mock_loop)
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service.get_platforms()
        assert result["platforms"][0]["sync_enabled"] is True

    @pytest.mark.asyncio
    async def test_http_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Connection refused"))
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is False
        assert "error_code" in result

    @pytest.mark.asyncio
    async def test_unexpected_response_type(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value="not a list")
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is False
        assert result["error_code"] == "api_error"


class TestSavePlatformSync:
    """Tests for save_platform_sync() — lines 120-123."""

    def test_saves_enabled_setting(self, plugin):
        result = plugin._sync_service.save_platform_sync(42, True)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["42"] is True

    def test_saves_disabled_setting(self, plugin):
        plugin.settings["enabled_platforms"]["42"] = True
        result = plugin._sync_service.save_platform_sync(42, False)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["42"] is False


class TestSetAllPlatformsSync:
    """Tests for set_all_platforms_sync() — lines 126-139."""

    @pytest.mark.asyncio
    async def test_enables_all(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64"},
                {"id": 2, "name": "SNES"},
            ]
        )
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.set_all_platforms_sync(True)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["1"] is True
        assert plugin.settings["enabled_platforms"]["2"] is True

    @pytest.mark.asyncio
    async def test_disables_all(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"id": 1, "name": "N64"}])
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.set_all_platforms_sync(False)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["1"] is False

    @pytest.mark.asyncio
    async def test_http_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("timeout"))
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.set_all_platforms_sync(True)
        assert result["success"] is False


class TestGetCollections:
    """Tests for LibraryService.get_collections()."""

    @pytest.mark.asyncio
    async def test_returns_user_smart_and_franchise_collections(self, plugin):
        """User, smart, and franchise collections all appear in the result."""
        user = [{"id": 1, "name": "My Faves", "rom_count": 3, "is_favorite": False}]
        smart = [{"id": 5, "name": "Recent Adds", "rom_count": 12}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 5, "is_favorite": False}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, smart, franchise))

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        collections = result["collections"]
        names = [c["name"] for c in collections]
        assert "My Faves" in names
        assert "Recent Adds" in names
        assert "Mario" in names

    @pytest.mark.asyncio
    async def test_user_collection_has_user_kind(self, plugin):
        """Non-favorite user collections carry kind='user' and is_favorite=False."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["kind"] == "user"
        assert result["collections"][0]["is_favorite"] is False

    @pytest.mark.asyncio
    async def test_franchise_collection_has_franchise_kind(self, plugin):
        """Franchise collections carry kind='franchise'."""
        franchise = [{"id": 101, "name": "Zelda", "rom_count": 4}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor([], [], franchise))

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["kind"] == "franchise"

    @pytest.mark.asyncio
    async def test_smart_collection_has_smart_kind(self, plugin):
        """Smart collections carry kind='smart' and is_favorite=False."""
        smart = [{"id": 7, "name": "Filter A", "rom_count": 10}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor([], smart, []))

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["kind"] == "smart"
        assert result["collections"][0]["is_favorite"] is False

    @pytest.mark.asyncio
    async def test_kind_order_user_smart_franchise(self, plugin):
        """User collections precede smart, which precede franchise (sort order)."""
        user = [{"id": 1, "name": "U1", "rom_count": 1, "is_favorite": False}]
        smart = [{"id": 5, "name": "S1", "rom_count": 1}]
        franchise = [{"id": 101, "name": "F1", "rom_count": 1}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, smart, franchise))

        result = await plugin._sync_service.get_collections()

        kinds = [c["kind"] for c in result["collections"]]
        user_idx = kinds.index("user")
        smart_idx = kinds.index("smart")
        franchise_idx = kinds.index("franchise")
        assert user_idx < smart_idx < franchise_idx

    @pytest.mark.asyncio
    async def test_favorite_collection_has_is_favorite_true(self, plugin):
        """Collections with is_favorite=True carry kind='user' and is_favorite=True."""
        user = [{"id": 1, "name": "Top Picks", "rom_count": 5, "is_favorite": True}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["kind"] == "user"
        assert result["collections"][0]["is_favorite"] is True

    @pytest.mark.asyncio
    async def test_respects_enabled_settings(self, plugin):
        """sync_enabled reflects the per-bucket enabled_collections setting."""
        user = [
            {"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False},
            {"id": 2, "name": "Shooters", "rom_count": 3, "is_favorite": False},
        ]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))
        plugin._sync_service._settings["enabled_collections"] = {
            "user": {"1": True, "2": False},
            "smart": {},
            "franchise": {},
        }

        result = await plugin._sync_service.get_collections()

        by_id = {c["id"]: c for c in result["collections"]}
        assert by_id["1"]["sync_enabled"] is True
        assert by_id["2"]["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_respects_smart_bucket_enabled_settings(self, plugin):
        """Smart-collection sync_enabled comes from the smart bucket only."""
        smart = [{"id": 7, "name": "Filter A", "rom_count": 1}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor([], smart, []))
        plugin._sync_service._settings["enabled_collections"] = {
            "user": {"7": True},  # same id under a different bucket — must not leak
            "smart": {"7": False},
            "franchise": {},
        }

        result = await plugin._sync_service.get_collections()

        smart_entry = next(c for c in result["collections"] if c["kind"] == "smart")
        assert smart_entry["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_defaults_to_disabled_when_no_settings(self, plugin):
        """When enabled_collections is absent all collections default to sync_enabled=False."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        franchise = [{"id": 101, "name": "Zelda", "rom_count": 3}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], franchise))
        plugin._sync_service._settings.pop("enabled_collections", None)

        result = await plugin._sync_service.get_collections()

        for c in result["collections"]:
            assert c["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_api_error_returns_error_response(self, plugin):
        """When list_collections raises an exception the response has success=False."""
        rebind_loop(plugin._sync_service, _make_loop_raising(Exception("Connection refused")))

        result = await plugin._sync_service.get_collections()

        assert result["success"] is False
        assert "error_code" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_empty_collections(self, plugin):
        """All endpoints returning [] still yields success=True with empty list."""
        rebind_loop(plugin._sync_service, _make_loop_with_executor([], [], []))

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        assert result["collections"] == []

    @pytest.mark.asyncio
    async def test_franchise_failure_still_returns_user_collections(self, plugin):
        """If franchise fetch fails, user + smart collections are still returned."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]

        mock_loop = MagicMock()
        call_count = 0

        async def _executor(_executor_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return user
            if call_count == 2:
                return []  # smart
            raise Exception("Franchise endpoint unavailable")

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        assert len(result["collections"]) == 1
        assert result["collections"][0]["name"] == "RPGs"

    @pytest.mark.asyncio
    async def test_smart_failure_still_returns_user_and_franchise(self, plugin):
        """If smart fetch fails, user + franchise collections still come through."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 3}]

        mock_loop = MagicMock()
        call_count = 0

        async def _executor(_executor_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return user
            if call_count == 2:
                raise Exception("Smart endpoint unavailable")
            return franchise

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        names = [c["name"] for c in result["collections"]]
        assert "RPGs" in names
        assert "Mario" in names

    @pytest.mark.asyncio
    async def test_rom_count_falls_back_to_rom_ids_length(self, plugin):
        """When rom_count is absent, len(rom_ids) is used."""
        user = [{"id": 1, "name": "RPGs", "rom_ids": [10, 20, 30], "is_favorite": False}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["rom_count"] == 3

    @pytest.mark.asyncio
    async def test_collections_sorted_alphabetically_within_kind(self, plugin):
        """Within a kind, collections are sorted by name (case-insensitive)."""
        user = [
            {"id": 2, "name": "Zelda", "rom_count": 1, "is_favorite": False},
            {"id": 1, "name": "Metroid", "rom_count": 1, "is_favorite": False},
        ]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        result = await plugin._sync_service.get_collections()

        names = [c["name"] for c in result["collections"]]
        assert names == ["Metroid", "Zelda"]

    @pytest.mark.asyncio
    async def test_collection_id_is_string(self, plugin):
        """IDs are always returned as strings regardless of the API response type."""
        user = [{"id": 42, "name": "Favorites", "rom_count": 1, "is_favorite": False}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["id"] == "42"


# ---------------------------------------------------------------------------
# TestSaveCollectionSync
# ---------------------------------------------------------------------------


class TestSaveCollectionSync:
    """Tests for LibraryService.save_collection_sync() — synchronous method."""

    def test_saves_enabled_user(self, plugin):
        """Enabling a user collection stores True under enabled_collections.user."""
        plugin._sync_service.save_collection_sync("42", "user", True)

        assert plugin._sync_service._settings["enabled_collections"]["user"]["42"] is True

    def test_saves_enabled_smart(self, plugin):
        """Enabling a smart collection stores True under enabled_collections.smart."""
        plugin._sync_service.save_collection_sync("7", "smart", True)

        assert plugin._sync_service._settings["enabled_collections"]["smart"]["7"] is True

    def test_saves_enabled_franchise(self, plugin):
        """Enabling a franchise collection stores True under enabled_collections.franchise."""
        b64 = "eyJuYW1lIjogIk1hcmlvIn0="
        plugin._sync_service.save_collection_sync(b64, "franchise", True)

        assert plugin._sync_service._settings["enabled_collections"]["franchise"][b64] is True

    def test_saves_disabled(self, plugin):
        """Disabling a previously-enabled collection stores False in the right bucket."""
        plugin._sync_service._settings["enabled_collections"] = {
            "user": {"42": True},
            "smart": {},
            "franchise": {},
        }

        plugin._sync_service.save_collection_sync("42", "user", False)

        assert plugin._sync_service._settings["enabled_collections"]["user"]["42"] is False

    def test_returns_success(self, plugin):
        result = plugin._sync_service.save_collection_sync("1", "user", True)

        assert result == {"success": True}

    def test_rejects_invalid_kind(self, plugin):
        """Passing an unknown kind returns success=False without writing."""
        result = plugin._sync_service.save_collection_sync("1", "bogus", True)

        assert result["success"] is False
        assert result["reason"] == "invalid_kind"
        assert "Invalid collection kind" in result["message"]

    def test_string_id_stored_from_int(self, plugin):
        """Passing an integer id is coerced to a string key."""
        plugin._sync_service.save_collection_sync(99, "user", True)

        assert "99" in plugin._sync_service._settings["enabled_collections"]["user"]
        assert plugin._sync_service._settings["enabled_collections"]["user"]["99"] is True

    def test_creates_enabled_collections_key_if_absent(self, plugin):
        """enabled_collections is created with all three buckets if absent."""
        plugin._sync_service._settings.pop("enabled_collections", None)

        plugin._sync_service.save_collection_sync("7", "smart", True)

        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["smart"]["7"] is True
        assert ec["user"] == {}
        assert ec["franchise"] == {}

    def test_calls_save_settings(self, plugin):
        """settings_persister is triggered after updating the setting."""
        recorder = FakeSettingsPersister()
        plugin._sync_service._fetcher._settings_persister = recorder

        plugin._sync_service.save_collection_sync("1", "user", True)

        assert recorder.save_count == 1

    def test_does_not_call_save_settings_on_invalid_kind(self, plugin):
        """Invalid kind short-circuits before persistence."""
        recorder = FakeSettingsPersister()
        plugin._sync_service._fetcher._settings_persister = recorder

        plugin._sync_service.save_collection_sync("1", "bogus", True)

        assert recorder.save_count == 0


# ---------------------------------------------------------------------------
# TestSetAllCollectionsSync
# ---------------------------------------------------------------------------


class TestSetAllCollectionsSync:
    """Tests for LibraryService.set_all_collections_sync()."""

    @pytest.mark.asyncio
    async def test_enable_all(self, plugin):
        """Calling with enabled=True scope=None marks every collection enabled in its bucket."""
        user = [
            {"id": 1, "name": "RPGs", "is_favorite": False},
            {"id": 2, "name": "Action", "is_favorite": False},
        ]
        smart = [{"id": 5, "name": "Filter A"}]
        franchise = [{"id": 101, "name": "Mario"}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, smart, franchise))

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["user"]["1"] is True
        assert ec["user"]["2"] is True
        assert ec["smart"]["5"] is True
        assert ec["franchise"]["101"] is True

    @pytest.mark.asyncio
    async def test_disable_all(self, plugin):
        """Calling with enabled=False scope=None marks every collection disabled."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        smart = [{"id": 5, "name": "Filter"}]
        franchise = [{"id": 101, "name": "Mario"}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, smart, franchise))
        plugin._sync_service._settings["enabled_collections"] = {
            "user": {"1": True},
            "smart": {"5": True},
            "franchise": {"101": True},
        }

        result = await plugin._sync_service.set_all_collections_sync(False)

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["user"]["1"] is False
        assert ec["smart"]["5"] is False
        assert ec["franchise"]["101"] is False

    @pytest.mark.asyncio
    async def test_filter_by_franchise_scope(self, plugin):
        """Passing scope='franchise' only touches franchise collections."""
        franchise = [{"id": 101, "name": "Mario"}]
        # Only franchise is fetched when scope='franchise'.
        rebind_loop(plugin._sync_service, _make_loop_with_executor(franchise))

        result = await plugin._sync_service.set_all_collections_sync(True, scope="franchise")

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["franchise"]["101"] is True
        assert ec["user"] == {}
        assert ec["smart"] == {}

    @pytest.mark.asyncio
    async def test_filter_by_smart_scope(self, plugin):
        """Passing scope='smart' only touches smart collections."""
        smart = [{"id": 7, "name": "Filter A"}, {"id": 8, "name": "Filter B"}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(smart))

        result = await plugin._sync_service.set_all_collections_sync(True, scope="smart")

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["smart"]["7"] is True
        assert ec["smart"]["8"] is True
        assert ec["user"] == {}
        assert ec["franchise"] == {}

    @pytest.mark.asyncio
    async def test_filter_by_my_scope(self, plugin):
        """Passing scope='my' only touches non-favorite user collections."""
        user = [
            {"id": 1, "name": "RPGs", "is_favorite": False},
            {"id": 2, "name": "Faves", "is_favorite": True},
        ]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user))

        result = await plugin._sync_service.set_all_collections_sync(True, scope="my")

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["user"]["1"] is True
        assert "2" not in ec["user"]
        assert ec["smart"] == {}
        assert ec["franchise"] == {}

    @pytest.mark.asyncio
    async def test_rejects_invalid_scope(self, plugin):
        """Unknown scope short-circuits with success=False, no API call."""
        result = await plugin._sync_service.set_all_collections_sync(True, scope="bogus")
        assert result["success"] is False
        assert result["reason"] == "invalid_scope"
        assert "Invalid scope" in result["message"]

    @pytest.mark.asyncio
    async def test_rejects_favorites_scope(self, plugin):
        """scope='favorites' is no longer a valid sub-scope — favorites is a top-level toggle."""
        result = await plugin._sync_service.set_all_collections_sync(True, scope="favorites")
        assert result["success"] is False
        assert result["reason"] == "invalid_scope"
        assert "Invalid scope" in result["message"]

    @pytest.mark.asyncio
    async def test_api_error_returns_error_response(self, plugin):
        """When list_collections raises, the response has success=False."""
        rebind_loop(plugin._sync_service, _make_loop_raising(Exception("timeout")))

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is False
        assert "error_code" in result

    @pytest.mark.asyncio
    async def test_smart_scope_api_error_returns_error_response(self, plugin):
        """When scope='smart' and list_smart_collections raises, surface the failure."""
        rebind_loop(plugin._sync_service, _make_loop_raising(Exception("smart endpoint down")))

        result = await plugin._sync_service.set_all_collections_sync(True, scope="smart")

        assert result["success"] is False
        assert "error_code" in result
        assert "message" in result
        # Settings must not be mutated when the single-scope fetch fails.
        assert plugin._sync_service._settings["enabled_collections"]["smart"] == {}

    @pytest.mark.asyncio
    async def test_franchise_scope_api_error_returns_error_response(self, plugin):
        """When scope='franchise' and list_virtual_collections raises, surface the failure."""
        rebind_loop(plugin._sync_service, _make_loop_raising(Exception("franchise endpoint down")))

        result = await plugin._sync_service.set_all_collections_sync(True, scope="franchise")

        assert result["success"] is False
        assert "error_code" in result
        assert "message" in result
        # Settings must not be mutated when the single-scope fetch fails.
        assert plugin._sync_service._settings["enabled_collections"]["franchise"] == {}

    @pytest.mark.asyncio
    async def test_franchise_failure_still_processes_user_and_smart(self, plugin):
        """If franchise fetch fails, user + smart collections are still processed."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        smart = [{"id": 5, "name": "Filter"}]

        mock_loop = MagicMock()
        call_count = 0

        async def _executor(_executor_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return user
            if call_count == 2:
                return smart
            raise Exception("Franchise endpoint unavailable")

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["user"]["1"] is True
        assert ec["smart"]["5"] is True

    @pytest.mark.asyncio
    async def test_smart_failure_still_processes_user_and_franchise(self, plugin):
        """If smart fetch fails, user + franchise still go through."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario"}]

        mock_loop = MagicMock()
        call_count = 0

        async def _executor(_executor_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return user
            if call_count == 2:
                raise Exception("Smart endpoint unavailable")
            return franchise

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        rebind_loop(plugin._sync_service, mock_loop)

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["user"]["1"] is True
        assert ec["franchise"]["101"] is True

    @pytest.mark.asyncio
    async def test_calls_save_settings(self, plugin):
        """settings_persister is triggered after updating collections."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        recorder = FakeSettingsPersister()
        plugin._sync_service._fetcher._settings_persister = recorder

        await plugin._sync_service.set_all_collections_sync(True)

        assert recorder.save_count == 1

    @pytest.mark.asyncio
    async def test_enabled_param_coerced_to_bool(self, plugin):
        """Truthy/falsy values are coerced to bool."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, [], []))

        await plugin._sync_service.set_all_collections_sync(1)  # truthy int

        assert plugin._sync_service._settings["enabled_collections"]["user"]["1"] is True

    @pytest.mark.asyncio
    async def test_scope_none_processes_all_buckets(self, plugin):
        """When scope is None (default), all three buckets are processed."""
        user = [
            {"id": 1, "name": "Faves", "is_favorite": True},
            {"id": 2, "name": "RPGs", "is_favorite": False},
        ]
        smart = [{"id": 5, "name": "Filter"}]
        franchise = [{"id": 101, "name": "Mario"}]
        rebind_loop(plugin._sync_service, _make_loop_with_executor(user, smart, franchise))

        await plugin._sync_service.set_all_collections_sync(True, scope=None)

        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["user"]["1"] is True
        assert ec["user"]["2"] is True
        assert ec["smart"]["5"] is True
        assert ec["franchise"]["101"] is True


# ---------------------------------------------------------------------------
# TestGetCollectionsUnsupported / TestSetAllCollectionsSyncUnsupported
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestFetchCollectionRoms
# ---------------------------------------------------------------------------


def _seed_platform_names(uow, names: dict[str, str]) -> None:
    """Seed the offline ``platform_slug → display_name`` cache."""
    import json

    with uow:
        uow.kv_config.set("platform_names", json.dumps(names))


class TestRemoveAllShortcuts:
    @pytest.mark.asyncio
    async def test_returns_app_ids_and_rom_ids(self, plugin):
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(plugin._uow, 20, app_id=1002, platform_slug="n64", name="Game B")
        _seed_rom(plugin._uow, 30, app_id=None, platform_slug="snes", name="Game C")  # unbound (edge)

        result = await plugin.remove_all_shortcuts()
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20", "30"}

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        result = await plugin.remove_all_shortcuts()
        assert result["success"] is True
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    @pytest.mark.asyncio
    async def test_does_not_unbind_roms(self, plugin):
        """remove_all_shortcuts just returns data; unbinding happens in report_removal_results."""
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        await plugin.remove_all_shortcuts()
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id == 1001


class TestReportRemovalResults:
    @pytest.mark.asyncio
    async def test_unbinds_removed_roms_but_keeps_rows(self, plugin):
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(plugin._uow, 20, app_id=1002, platform_slug="n64", name="Game B")

        result = await plugin.report_removal_results([10, 20])
        assert result["success"] is True
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id is None
            assert uow.roms.get(20).shortcut_app_id is None

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_cover_path(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        art_file = tmp_path / "cover.png"
        art_file.write_text("fake")
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        with plugin._uow as uow:
            rom = uow.roms.get(10)
            rom.update_cover_path(str(art_file))
            uow.roms.save(rom)
        plugin._steam_config.grid_dir = lambda: str(tmp_path)

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_app_id(self, plugin, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "1001p.png"
        art_file.write_text("fake")
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_partial_removal(self, plugin):
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(plugin._uow, 20, app_id=1002, platform_slug="n64", name="Game B")

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id is None
            assert uow.roms.get(20).shortcut_app_id == 1002


class TestRemovePlatformShortcuts:
    @pytest.mark.asyncio
    async def test_returns_matching_platform_entries(self, plugin):
        _seed_platform_names(plugin._uow, {"n64": "Nintendo 64", "snes": "Super Nintendo"})
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        _seed_rom(plugin._uow, 20, app_id=1002, platform_slug="n64", name="Zelda OOT")
        _seed_rom(plugin._uow, 30, app_id=1003, platform_slug="snes", name="DKC")

        result = await plugin.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20"}
        assert result["platform_name"] == "Nintendo 64"

    @pytest.mark.asyncio
    async def test_platform_with_no_roms(self, plugin):
        """A slug with no synced ROMs returns empty sets; name degrades to the slug."""
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")

        result = await plugin.remove_platform_shortcuts("nonexistent")
        assert result["success"] is True
        assert result["app_ids"] == []
        assert result["rom_ids"] == []
        assert result["platform_name"] == "nonexistent"

    @pytest.mark.asyncio
    async def test_does_not_unbind_roms(self, plugin):
        """remove_platform_shortcuts just returns data; unbinding happens in report_removal_results."""
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")

        await plugin.remove_platform_shortcuts("n64")
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id == 1001

    @pytest.mark.asyncio
    async def test_resolves_name_from_cache(self, plugin):
        """The display name comes from the kv_config cache, working offline."""
        _seed_platform_names(plugin._uow, {"n64": "Nintendo 64"})
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        _seed_rom(plugin._uow, 20, app_id=1002, platform_slug="n64", name="Zelda OOT")

        result = await plugin.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert result["platform_name"] == "Nintendo 64"


class TestRemovalCleansUpAppIdArtwork:
    """Tests for app_id-based artwork cleanup in report_removal_results."""

    @pytest.mark.asyncio
    async def test_removes_app_id_artwork(self, plugin, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "100001p.png"
        art_file.write_text("fake")
        _seed_rom(plugin._uow, 10, app_id=100001, platform_slug="n64", name="Game A")
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        await plugin.report_removal_results([10])
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_removes_staging_leftover(self, plugin, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_10_cover.png"
        staging.write_text("fake")
        _seed_rom(plugin._uow, 10, app_id=100001, platform_slug="n64", name="Game A")
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        await plugin.report_removal_results([10])
        assert not staging.exists()


class TestReportRemovalSteamInputCleanup:
    """Tests for Steam Input cleanup in _report_removal_results_io."""

    @pytest.mark.asyncio
    async def test_cleans_steam_input_on_removal(self, plugin, tmp_path):
        plugin._steam_config.grid_dir = lambda: str(tmp_path)
        plugin._steam_config.set_steam_input_config = MagicMock()
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(plugin._uow, 20, app_id=1002, platform_slug="n64", name="Game B")

        await plugin.report_removal_results([10, 20])
        plugin._steam_config.set_steam_input_config.assert_called_once_with([1001, 1002], mode="default")

    @pytest.mark.asyncio
    async def test_steam_input_error_doesnt_crash(self, plugin, tmp_path):
        plugin._steam_config.grid_dir = lambda: str(tmp_path)
        plugin._steam_config.set_steam_input_config = MagicMock(side_effect=Exception("VDF error"))
        _seed_rom(plugin._uow, 10, app_id=1001, platform_slug="n64", name="Game A")

        result = await plugin.report_removal_results([10])
        assert result["success"] is True  # Should not crash
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id is None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class TestCollectionSyncEdgeCases:
    """Edge-case tests for the merged platform + collection sync engine.

    Tests exercise classify_roms() and _build_collection_app_ids() directly.
    """

    # ------------------------------------------------------------------
    # Scenario 1: Platform disabled, collection keeps game alive
    # ------------------------------------------------------------------

    def test_sc1_collection_keeps_rom_alive_when_platform_disabled(self, plugin):
        """ROM A stays because Favorites collection references it; ROM B becomes stale.

        Platform GBA is disabled between sync 1 and sync 2. The registry has
        both ROM A (id=1) and ROM B (id=2) from the previous sync. On sync 2,
        only ROM A appears in shortcuts_data (via collection). ROM B has no
        source and must be classified as stale.
        """
        # Registry after first sync
        registry = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001),
            "2": _make_registry_entry("ROM B", "Game Boy Advance", app_id=1002),
        }

        # Second sync: GBA platform is disabled, Favorites collection keeps ROM A
        # shortcuts_data only contains ROM A (fetched via collection)
        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
            }
        ]
        # GBA is not in fetched platform names (platform disabled)
        fetched_platform_names = set()

        new, _changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, registry, fetched_platform_names
        )

        assert 1 in unchanged_ids, "ROM A should be unchanged (collection keeps it alive)"
        assert 2 in stale, "ROM B should be stale (no source references it)"
        assert len(new) == 0
        assert len(_changed) == 0

    # ------------------------------------------------------------------
    # Scenario 2: Collection disabled, platform keeps game alive
    # ------------------------------------------------------------------

    def test_sc2_platform_keeps_rom_alive_when_collection_disabled(self, plugin):
        """ROM A stays (platform reference); ROM C becomes stale (collection-only, now disabled).

        Platform GBA enabled → ROM A stays. PSX not enabled and Favorites
        collection disabled → ROM C has no source and is stale.
        """
        # Registry after first sync: ROM A (GBA via platform), ROM C (PSX via collection)
        registry = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
            "3": _make_registry_entry("ROM C", "PlayStation", app_id=1003, platform_slug="psx"),
        }

        # Second sync: Favorites disabled, GBA still enabled
        # shortcuts_data only contains ROM A from the GBA platform
        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
            }
        ]
        fetched_platform_names = {"Game Boy Advance"}

        new, _changed, unchanged_ids, stale, disabled_count = classify_roms(
            shortcuts_data, registry, fetched_platform_names
        )

        assert 1 in unchanged_ids, "ROM A should be unchanged (platform still enabled)"
        assert 3 in stale, "ROM C should be stale (collection disabled, PSX not enabled)"
        assert len(new) == 0
        # disabled_count: ROM C's platform (PlayStation) is NOT in fetched_platform_names
        assert disabled_count == 1

    # ------------------------------------------------------------------
    # Scenario 3: Game in multiple collections, one disabled
    # ------------------------------------------------------------------

    def test_sc3_rom_stays_alive_when_one_of_two_collections_disabled(self, plugin):
        """ROM A stays because RPG collection still references it even after Favorites is disabled."""
        registry = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001),
        }

        # ROM A still appears in shortcuts_data (RPG collection enabled)
        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
            }
        ]
        fetched_platform_names = set()

        _new, _changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, registry, fetched_platform_names
        )

        assert 1 in unchanged_ids, "ROM A should stay alive via RPG collection"
        assert len(stale) == 0

    # ------------------------------------------------------------------
    # Scenario 5/6: collection_create_platform_groups toggle via
    # _build_collection_app_ids (kept helper used by per-unit path)
    # ------------------------------------------------------------------

    def test_sc5c_build_collection_app_ids_excludes_collection_only_roms(self, plugin):
        """_build_collection_app_ids respects the toggle.

        Platform collection mapping is built from the full ``roms`` table
        by the per-unit finalisation path. Collection-only ROMs must be
        excluded when the toggle is OFF.
        """
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = False
        svc._settings["enabled_collections"] = {"user": {"3": True}, "smart": {}, "franchise": {}}

        # roms: ROM 1 from platform, ROM 2 from collection only
        _seed_rom(plugin._uow, 1, app_id=1001, platform_slug="gba", name="ROM A")
        _seed_rom(plugin._uow, 2, app_id=1002, platform_slug="psx", name="ROM B")
        names = {"gba": "Game Boy Advance", "psx": "PlayStation"}
        platform_rom_ids = {1}  # Only ROM 1 from platform

        with plugin._uow as uow:
            platform_app_ids, _ = svc._reporter._build_collection_app_ids(
                uow, platform_rom_ids, {"Favorites": [1, 2]}, names
            )

        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]
        assert "PlayStation" not in platform_app_ids, "PSX should be excluded (collection-only, toggle OFF)"

    def test_sc6c_build_collection_app_ids_includes_all_when_toggle_on(self, plugin):
        """Same as sc5c but with toggle ON — PSX should be included."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = True

        _seed_rom(plugin._uow, 1, app_id=1001, platform_slug="gba", name="ROM A")
        _seed_rom(plugin._uow, 2, app_id=1002, platform_slug="psx", name="ROM B")
        names = {"gba": "Game Boy Advance", "psx": "PlayStation"}
        platform_rom_ids = {1}

        with plugin._uow as uow:
            platform_app_ids, _ = svc._reporter._build_collection_app_ids(uow, platform_rom_ids, {}, names)

        assert "Game Boy Advance" in platform_app_ids
        assert "PlayStation" in platform_app_ids, "PSX should be included (toggle ON)"

    # ------------------------------------------------------------------
    # Scenario 7: Deduplication — ROM in both platform and collection
    # ------------------------------------------------------------------

    def test_sc7_rom_appears_in_both_platform_and_collection_app_ids(self, plugin):
        """ROM A (in both GBA platform and Favorites collection) appears in both
        platform_app_ids and romm_collection_app_ids when built via
        _build_collection_app_ids."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = False

        _seed_rom(plugin._uow, 1, app_id=1001, platform_slug="gba", name="ROM A")
        names = {"gba": "Game Boy Advance"}
        platform_rom_ids = {1}
        collection_memberships = {"Favorites": [1]}

        with plugin._uow as uow:
            platform_app_ids, romm_collection_app_ids = svc._reporter._build_collection_app_ids(
                uow, platform_rom_ids, collection_memberships, names
            )

        # Platform group for GBA exists (ROM A is a platform ROM)
        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]

        # Favorites collection app_ids also contains ROM A
        assert "Favorites" in romm_collection_app_ids
        assert 1001 in romm_collection_app_ids["Favorites"]

    # ------------------------------------------------------------------
    # Scenario 8: All sources removed — game gets stale
    # ------------------------------------------------------------------

    def test_sc8_rom_becomes_stale_when_no_source_references_it(self, plugin):
        """ROM A classified as stale when neither platform nor collection brings it in."""
        registry = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001),
        }

        # Empty shortcuts_data — no ROM was fetched from any source
        shortcuts_data: list = []
        fetched_platform_names: set = set()

        new, changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, registry, fetched_platform_names
        )

        assert 1 in stale
        assert len(new) == 0
        assert len(changed) == 0
        assert len(unchanged_ids) == 0

    # ------------------------------------------------------------------
    # Additional edge cases for _build_collection_app_ids
    # ------------------------------------------------------------------

    def test_build_collection_app_ids_empty_when_no_memberships(self, plugin):
        """romm_collection_app_ids is empty when no collection memberships are set."""
        svc = plugin._sync_service

        _seed_rom(plugin._uow, 1, app_id=1001, platform_slug="gba", name="ROM A")

        with plugin._uow as uow:
            _platform_app_ids, romm_collection_app_ids = svc._reporter._build_collection_app_ids(
                uow, {1}, {}, {"gba": "GBA"}
            )

        assert romm_collection_app_ids == {}

    def test_build_collection_app_ids_excludes_missing_registry_entries(self, plugin):
        """romm_collection_app_ids skips rom_ids that have no roms row."""
        svc = plugin._sync_service

        # Only ROM id=1 is in roms; ROM id=99 is referenced in memberships but missing.
        _seed_rom(plugin._uow, 1, app_id=1001, platform_slug="gba", name="ROM A")

        with plugin._uow as uow:
            _platform_app_ids, romm_collection_app_ids = svc._reporter._build_collection_app_ids(
                uow, {1}, {"Favorites": [1, 99]}, {"gba": "GBA"}
            )

        assert "Favorites" in romm_collection_app_ids
        assert 1001 in romm_collection_app_ids["Favorites"]
        # ROM 99 has no roms row, so its app_id is not included
        assert len(romm_collection_app_ids["Favorites"]) == 1

    def test_classify_roms_new_when_not_in_registry(self, plugin):
        """ROMs not present in the registry at all are classified as new."""
        registry = {}

        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "GBA",
                "platform_slug": "gba",
            }
        ]

        new, changed, unchanged_ids, stale, _disabled_count = classify_roms(shortcuts_data, registry, {"GBA"})

        assert len(new) == 1
        assert new[0]["rom_id"] == 1
        assert len(changed) == 0
        assert len(unchanged_ids) == 0
        assert len(stale) == 0

    def test_classify_roms_changed_when_name_differs(self, plugin):
        """ROMs whose name changed since last sync are classified as changed."""
        registry = {
            "1": _make_registry_entry("Old Name", "GBA", app_id=1001),
        }

        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "New Name",  # name changed
                "fs_name": "Old Name.zip",
                "platform_name": "GBA",
                "platform_slug": "gba",
            }
        ]

        new, changed, unchanged_ids, _stale, _disabled_count = classify_roms(shortcuts_data, registry, {"GBA"})

        assert len(changed) == 1
        assert changed[0]["rom_id"] == 1
        assert changed[0]["existing_app_id"] == 1001
        assert len(new) == 0
        assert len(unchanged_ids) == 0
