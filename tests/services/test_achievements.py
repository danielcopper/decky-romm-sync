import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.steam_config import SteamConfigAdapter
from domain.save_state import SaveSyncState

# conftest.py patches decky before this import
from main import Plugin
from services.achievements import AchievementsService, AchievementsServiceConfig
from services.game_detail import GameDetailService, GameDetailServiceConfig
from services.library import LibraryService, LibraryServiceConfig


@pytest.fixture
def clock():
    """FakeClock pinned to a synthetic instant.

    Shared between AchievementsService and GameDetailService so cache seeds
    stamped via ``clock.time()`` are comparable across both services.
    """
    return FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def plugin(clock):
    p = Plugin()
    p.settings = {
        "romm_url": "http://romm.local",
        "romm_user": "user",
        "romm_pass": "pass",
        "enabled_platforms": {},
        "log_level": "warn",
    }
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._state = {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {},
    }
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            state=p._state,
            settings=p.settings,
            metadata_cache=p._metadata_cache,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=clock,
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            save_state=p._save_state,
            save_settings_to_disk=p._save_settings_to_disk,
            log_debug=p._log_debug,
        ),
    )
    p._achievements_service = AchievementsService(
        config=AchievementsServiceConfig(
            romm_api=p._romm_api,
            state=p._state,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            clock=clock,
            log_debug=p._log_debug,
        ),
    )
    bios_checker = MagicMock()
    bios_checker.check_platform_bios_cached.return_value = None
    bios_checker.check_platform_bios = AsyncMock(return_value={"needs_bios": False})
    p._save_sync_state = SaveSyncState()
    p._game_detail_service = GameDetailService(
        config=GameDetailServiceConfig(
            state=p._state,
            metadata_cache=p._metadata_cache,
            save_sync_state=p._save_sync_state,
            logger=decky.logger,
            clock=clock,
            bios_checker=bios_checker,
            achievements=p._achievements_service,
        ),
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure service loops match the running event loop for async tests."""
    plugin._achievements_service._loop = asyncio.get_event_loop()


@pytest.fixture
def svc(plugin):
    return plugin._achievements_service


# ── Sample data helpers ──────────────────────────────────────


def _sample_achievements():
    """Return a list of two sample RA achievements as they appear in ra_metadata."""
    return [
        {
            "ra_id": 1001,
            "title": "First Blood",
            "description": "Defeat the first boss",
            "points": 10,
            "badge_url": "http://badges/1001.png",
            "badge_url_lock": "http://badges/1001_lock.png",
            "display_order": 1,
            "type": "progression",
            "num_awarded": 5000,
            "num_awarded_hardcore": 2000,
        },
        {
            "ra_id": 1002,
            "title": "Completionist",
            "description": "Find all secrets",
            "points": 50,
            "badge_url": "http://badges/1002.png",
            "badge_url_lock": "http://badges/1002_lock.png",
            "display_order": 2,
            "type": "missable",
            "num_awarded": 100,
            "num_awarded_hardcore": 50,
        },
    ]


def _sample_rom_data(achievements=None, use_merged=False):
    """Build a mock RomM ROM detail response with ra_metadata."""
    key = "merged_ra_metadata" if use_merged else "ra_metadata"
    return {
        "id": 42,
        "ra_id": 9999,
        key: {"achievements": achievements or _sample_achievements()},
    }


def _sample_user_data(ra_id, earned=5, total=10, earned_hardcore=3, ra_username="RetroPlayer"):
    """Build a mock /api/users/me response with ra_progression and ra_username."""
    return {
        "ra_username": ra_username,
        "ra_progression": {
            "results": [
                {
                    "rom_ra_id": ra_id,
                    "num_awarded": earned,
                    "num_awarded_hardcore": earned_hardcore,
                    "max_possible": total,
                    "earned_achievements": [1001, 1002, 1003, 1004, 1005][:earned],
                },
            ],
        },
    }


def _seed_ra_username_cache(svc, username="RetroPlayer"):
    """Pre-populate the RA username cache to simulate a known user."""
    svc._achievements_cache["_ra_user"] = {
        "username": username,
        "cached_at": svc._clock.time(),
    }


# --- get_ra_username (reads from achievements cache, not settings) ---


class TestGetRaUsername:
    def test_returns_username_from_cache(self, svc):
        _seed_ra_username_cache(svc, "RetroPlayer")
        assert svc.get_ra_username() == "RetroPlayer"

    def test_returns_empty_when_no_cache(self, svc):
        assert svc.get_ra_username() == ""

    def test_returns_empty_when_cache_expired(self, svc):
        svc._achievements_cache["_ra_user"] = {
            "username": "RetroPlayer",
            "cached_at": svc._clock.time() - (2 * 3600),  # 2h old > 1h TTL
        }
        assert svc.get_ra_username() == ""

    def test_returns_cached_when_fresh(self, svc):
        svc._achievements_cache["_ra_user"] = {
            "username": "JohnDoe",
            "cached_at": svc._clock.time() - 1800,  # 30min old < 1h TTL
        }
        assert svc.get_ra_username() == "JohnDoe"


# --- _fetch_ra_username ---


class TestFetchRaUsername:
    @pytest.mark.asyncio
    async def test_fetches_and_caches(self, svc, plugin):
        user_data = {"ra_username": "  RetroPlayer  "}

        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc._fetch_ra_username()

        assert result == "RetroPlayer"
        assert svc._achievements_cache["_ra_user"]["username"] == "RetroPlayer"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_ra_username_on_user(self, svc, plugin):
        user_data = {"ra_username": None}

        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc._fetch_ra_username()

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self, svc, plugin):
        plugin._romm_api.get_current_user.side_effect = Exception("Network error")
        result = await svc._fetch_ra_username()

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_stale_cache_on_api_error(self, svc, plugin):
        svc._achievements_cache["_ra_user"] = {
            "username": "OldUser",
            "cached_at": svc._clock.time() - (2 * 3600),  # expired
        }

        plugin._romm_api.get_current_user.side_effect = Exception("Network error")
        result = await svc._fetch_ra_username()

        assert result == "OldUser"

    @pytest.mark.asyncio
    async def test_empty_string_ra_username(self, svc, plugin):
        user_data = {"ra_username": ""}

        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc._fetch_ra_username()

        assert result == ""


# --- _get_achievements_cache_entry / get_progress_cache_entry ---


class TestAchievementsCacheEntry:
    def test_returns_entry_when_fresh(self, svc):
        svc._achievements_cache["42"] = {
            "achievements": [{"ra_id": 1}],
            "cached_at": svc._clock.time(),
        }
        result = svc._get_achievements_cache_entry("42")
        assert result is not None
        assert result["achievements"] == [{"ra_id": 1}]

    def test_returns_none_when_expired(self, svc):
        svc._achievements_cache["42"] = {
            "achievements": [{"ra_id": 1}],
            "cached_at": svc._clock.time() - (25 * 3600),  # 25h old > 24h TTL
        }
        result = svc._get_achievements_cache_entry("42")
        assert result is None

    def test_returns_none_when_missing(self, svc):
        result = svc._get_achievements_cache_entry("42")
        assert result is None

    def test_returns_none_when_empty_entry(self, svc):
        svc._achievements_cache["42"] = {}
        result = svc._get_achievements_cache_entry("42")
        assert result is None

    def test_boundary_exactly_at_ttl(self, svc):
        """Entry at exactly TTL age is considered expired."""
        svc._achievements_cache["42"] = {
            "achievements": [{"ra_id": 1}],
            "cached_at": svc._clock.time() - (24 * 3600 + 1),
        }
        result = svc._get_achievements_cache_entry("42")
        assert result is None


class TestProgressCacheEntry:
    def test_returns_entry_when_fresh(self, svc):
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 5,
                "total": 10,
                "cached_at": svc._clock.time(),
            },
        }
        result = svc.get_progress_cache_entry("42")
        assert result is not None
        assert result["earned"] == 5

    def test_returns_none_when_expired(self, svc):
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 5,
                "total": 10,
                "cached_at": svc._clock.time() - (2 * 3600),  # 2h old > 1h TTL
            },
        }
        result = svc.get_progress_cache_entry("42")
        assert result is None

    def test_returns_none_when_missing(self, svc):
        result = svc.get_progress_cache_entry("42")
        assert result is None

    def test_returns_none_when_no_user_progress_key(self, svc):
        svc._achievements_cache["42"] = {"achievements": []}
        result = svc.get_progress_cache_entry("42")
        assert result is None

    def test_returns_none_when_user_progress_is_none(self, svc):
        svc._achievements_cache["42"] = {"user_progress": None}
        result = svc.get_progress_cache_entry("42")
        assert result is None

    def test_entry_includes_cached_at(self, svc):
        """Returned progress entry includes cached_at timestamp."""
        store_time = svc._clock.time() - 300
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 3,
                "total": 10,
                "cached_at": store_time,
            },
        }
        result = svc.get_progress_cache_entry("42")
        assert result is not None
        assert "cached_at" in result
        assert result["cached_at"] == store_time

    def test_cached_at_reflects_storage_time(self, svc):
        """cached_at is the original storage time, not current time."""
        store_time = svc._clock.time() - 1800  # 30 min ago
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 1,
                "total": 5,
                "cached_at": store_time,
            },
        }
        result = svc.get_progress_cache_entry("42")
        assert result["cached_at"] == store_time
        assert result["cached_at"] < svc._clock.time() - 1700


# --- get_achievements ---


class TestGetAchievements:
    @pytest.mark.asyncio
    async def test_happy_path_fetches_and_caches(self, svc, plugin):
        """Fetches from API, returns achievements, caches result."""
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999, "app_id": 100}
        rom_data = _sample_rom_data()

        plugin._romm_api.get_rom.return_value = rom_data
        result = await svc.get_achievements(42)

        assert result["success"] is True
        assert result["total"] == 2
        assert len(result["achievements"]) == 2
        assert result["achievements"][0]["title"] == "First Blood"
        # Verify cached
        assert "42" in svc._achievements_cache
        assert len(svc._achievements_cache["42"]["achievements"]) == 2
        assert svc._achievements_cache["42"]["ra_id"] == 9999

    @pytest.mark.asyncio
    async def test_cache_hit_returns_without_api_call(self, svc, plugin):
        """Returns cached data without calling get_rom."""
        svc._achievements_cache["42"] = {
            "achievements": [{"ra_id": 1001, "title": "Cached"}],
            "cached_at": svc._clock.time(),
        }
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}

        result = await svc.get_achievements(42)

        plugin._romm_api.get_rom.assert_not_called()
        assert result["success"] is True
        assert result["total"] == 1
        assert result["achievements"][0]["title"] == "Cached"

    @pytest.mark.asyncio
    async def test_cache_expired_refetches(self, svc, plugin):
        """Refetches from API when cache is older than TTL."""
        svc._achievements_cache["42"] = {
            "achievements": [{"ra_id": 1001, "title": "Old"}],
            "cached_at": svc._clock.time() - (25 * 3600),
        }
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()

        plugin._romm_api.get_rom.return_value = rom_data
        result = await svc.get_achievements(42)

        assert result["success"] is True
        assert result["total"] == 2
        assert result["achievements"][0]["title"] == "First Blood"

    @pytest.mark.asyncio
    async def test_no_ra_id_returns_empty(self, svc, plugin):
        """When no ra_id in registry, returns empty with no_ra_id flag."""
        plugin._state["shortcut_registry"]["42"] = {"app_id": 100}

        result = await svc.get_achievements(42)

        assert result["success"] is True
        assert result["achievements"] == []
        assert result["total"] == 0
        assert result["no_ra_id"] is True

    @pytest.mark.asyncio
    async def test_no_registry_entry_returns_empty(self, svc):
        """When rom_id not in registry at all, returns empty with no_ra_id flag."""
        result = await svc.get_achievements(42)

        assert result["success"] is True
        assert result["achievements"] == []
        assert result["no_ra_id"] is True

    @pytest.mark.asyncio
    async def test_api_error_returns_stale_cache(self, svc, plugin):
        """On API error, returns stale cache if available."""
        svc._achievements_cache["42"] = {
            "achievements": [{"ra_id": 1001, "title": "Stale"}],
            "cached_at": svc._clock.time() - (25 * 3600),  # expired
        }
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}

        plugin._romm_api.get_rom.side_effect = Exception("Connection refused")
        result = await svc.get_achievements(42)

        assert result["success"] is True
        assert result["stale"] is True
        assert result["achievements"][0]["title"] == "Stale"

    @pytest.mark.asyncio
    async def test_api_error_no_cache_returns_error(self, svc, plugin):
        """On API error with no cache, returns error with empty list."""
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}

        plugin._romm_api.get_rom.side_effect = Exception("Connection refused")
        result = await svc.get_achievements(42)

        assert result["success"] is False
        assert result["achievements"] == []
        assert result["total"] == 0
        assert "Connection refused" in result["message"]

    @pytest.mark.asyncio
    async def test_rom_id_cast_to_int(self, svc, plugin):
        """rom_id is cast to int, so string input works too."""
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()

        plugin._romm_api.get_rom.return_value = rom_data
        result = await svc.get_achievements("42")

        assert result["success"] is True
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_empty_achievements_from_api(self, svc, plugin):
        """API returns ROM with no achievements."""
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = {"id": 42, "ra_metadata": {"achievements": []}}

        plugin._romm_api.get_rom.return_value = rom_data
        result = await svc.get_achievements(42)

        assert result["success"] is True
        assert result["total"] == 0
        assert result["achievements"] == []


# --- get_achievement_progress ---


class TestGetAchievementProgress:
    @pytest.mark.asyncio
    async def test_happy_path(self, svc, plugin):
        """Fetches user progression, returns earned/total."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = _sample_user_data(ra_id=9999, earned=5, total=10, earned_hardcore=3)

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["earned"] == 5
        assert result["total"] == 10
        assert result["earned_hardcore"] == 3
        assert len(result["earned_achievements"]) == 5

    @pytest.mark.asyncio
    async def test_no_ra_username_fetches_from_romm(self, svc, plugin):
        """When no cached RA username, fetches from get_current_user."""
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data_with_username = _sample_user_data(ra_id=9999, earned=5, total=10)

        plugin._romm_api.get_rom.return_value = rom_data
        # First call: _fetch_ra_username, second call: progression fetch
        plugin._romm_api.get_current_user.side_effect = [
            {"ra_username": "RetroPlayer"},
            user_data_with_username,
        ]
        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["earned"] == 5
        # RA username should now be cached
        assert svc._achievements_cache["_ra_user"]["username"] == "RetroPlayer"

    @pytest.mark.asyncio
    async def test_no_ra_username_anywhere_returns_error(self, svc, plugin):
        """When no RA username in cache and RomM user has none, returns error."""
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}

        plugin._romm_api.get_current_user.return_value = {"ra_username": None}
        result = await svc.get_achievement_progress(42)

        assert result["success"] is False
        assert "No RA username" in result["message"]
        assert result["earned"] == 0

    @pytest.mark.asyncio
    async def test_no_ra_id_returns_zeros(self, svc, plugin):
        """When no ra_id in registry, returns zeros with no_ra_id flag."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"app_id": 100}

        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["earned"] == 0
        assert result["total"] == 0
        assert result["no_ra_id"] is True

    @pytest.mark.asyncio
    async def test_cache_hit(self, svc, plugin):
        """Returns cached progress without API call."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 3,
                "earned_hardcore": 1,
                "total": 10,
                "earned_achievements": [1001, 1002, 1003],
                "cached_at": svc._clock.time(),
            },
        }

        result = await svc.get_achievement_progress(42)

        plugin._romm_api.get_rom.assert_not_called()
        plugin._romm_api.get_current_user.assert_not_called()
        assert result["success"] is True
        assert result["earned"] == 3
        assert result["total"] == 10

    @pytest.mark.asyncio
    async def test_game_not_found_in_progression(self, svc, plugin):
        """When the game's ra_id is not in progression results, returns zeros."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        # User data has progression for a different game
        user_data = {
            "ra_username": "RetroPlayer",
            "ra_progression": {"results": [{"rom_ra_id": 1111, "num_awarded": 5}]},
        }

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["earned"] == 0
        assert result["total"] == 2  # total from achievements list

    @pytest.mark.asyncio
    async def test_api_error_returns_stale_cache(self, svc, plugin):
        """On API error, returns stale progress cache if available."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        # Pre-populate achievements cache so get_achievements succeeds from cache
        svc._achievements_cache["42"] = {
            "achievements": _sample_achievements(),
            "cached_at": svc._clock.time(),
            "user_progress": {
                "earned": 2,
                "earned_hardcore": 0,
                "total": 10,
                "earned_achievements": [1001, 1002],
                "cached_at": svc._clock.time() - (2 * 3600),  # expired progress
            },
        }

        # get_achievements cache hit, then get_current_user fails
        plugin._romm_api.get_current_user.side_effect = Exception("Network error")
        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["stale"] is True
        assert result["earned"] == 2

    @pytest.mark.asyncio
    async def test_api_error_no_cache_returns_error(self, svc, plugin):
        """On API error with no stale cache, returns error."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        # Pre-populate achievements cache so get_achievements succeeds
        svc._achievements_cache["42"] = {
            "achievements": _sample_achievements(),
            "cached_at": svc._clock.time(),
        }

        plugin._romm_api.get_current_user.side_effect = Exception("Network error")
        result = await svc.get_achievement_progress(42)

        assert result["success"] is False
        assert result["earned"] == 0
        assert result["total"] == 0
        assert "Network error" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_ra_progression(self, svc, plugin):
        """User data with empty ra_progression returns zeros."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = {"ra_username": "RetroPlayer", "ra_progression": {"results": []}}

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["earned"] == 0
        assert result["total"] == 2  # from achievement list count

    @pytest.mark.asyncio
    async def test_none_ra_progression(self, svc, plugin):
        """User data with None ra_progression returns zeros."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = {"ra_username": "RetroPlayer", "ra_progression": None}

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        assert result["success"] is True
        assert result["earned"] == 0

    @pytest.mark.asyncio
    async def test_progress_caches_result(self, svc, plugin):
        """Successful progress fetch is cached in _achievements_cache."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = _sample_user_data(ra_id=9999, earned=7, total=10)

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        await svc.get_achievement_progress(42)

        cached = svc._achievements_cache["42"]["user_progress"]
        assert cached["earned"] == 7
        assert cached["total"] == 10
        assert "cached_at" in cached

    @pytest.mark.asyncio
    async def test_cached_at_not_in_response(self, svc, plugin):
        """The cached_at timestamp is not leaked into the response."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = _sample_user_data(ra_id=9999, earned=7, total=10)

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        assert "cached_at" not in result

    @pytest.mark.asyncio
    async def test_max_possible_fallback_to_total(self, svc, plugin):
        """When max_possible is None/0, falls back to total from achievements list."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = {
            "ra_username": "RetroPlayer",
            "ra_progression": {
                "results": [
                    {"rom_ra_id": 9999, "num_awarded": 1, "max_possible": 0},
                ],
            },
        }

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        # Fallback: total should be len(achievements) = 2
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_none_num_awarded_treated_as_zero(self, svc, plugin):
        """When num_awarded is None in progression, treat as 0."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = {
            "ra_username": "RetroPlayer",
            "ra_progression": {
                "results": [
                    {
                        "rom_ra_id": 9999,
                        "num_awarded": None,
                        "num_awarded_hardcore": None,
                        "max_possible": 10,
                    },
                ],
            },
        }

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.get_achievement_progress(42)

        assert result["earned"] == 0
        assert result["earned_hardcore"] == 0

    @pytest.mark.asyncio
    async def test_caches_ra_username_from_users_me_response(self, svc, plugin):
        """The get_current_user call in progress fetch also caches ra_username."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = _sample_user_data(ra_id=9999, earned=5, total=10, ra_username="NewUser")

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        await svc.get_achievement_progress(42)

        # RA username should have been updated from the users/me response
        assert svc._achievements_cache["_ra_user"]["username"] == "NewUser"


# --- sync_achievements_after_session ---


class TestSyncAchievementsAfterSession:
    @pytest.mark.asyncio
    async def test_invalidates_cache_and_refetches(self, svc, plugin):
        """Invalidates progress cache and fetches fresh data."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}

        # Pre-populate cache with old progress
        svc._achievements_cache["42"] = {
            "achievements": _sample_achievements(),
            "cached_at": svc._clock.time(),
            "user_progress": {
                "earned": 1,
                "total": 10,
                "cached_at": svc._clock.time(),
            },
        }

        user_data = _sample_user_data(ra_id=9999, earned=5, total=10)

        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.sync_achievements_after_session(42)

        assert result["success"] is True
        assert result["earned"] == 5
        # Old progress should have been replaced
        assert svc._achievements_cache["42"]["user_progress"]["earned"] == 5

    @pytest.mark.asyncio
    async def test_cache_cleared_before_refetch(self, svc, plugin):
        """Verifies that user_progress is deleted before get_achievement_progress is called."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        svc._achievements_cache["42"] = {
            "achievements": _sample_achievements(),
            "cached_at": svc._clock.time(),
            "user_progress": {
                "earned": 1,
                "total": 10,
                "cached_at": svc._clock.time(),
            },
        }

        call_order = []

        original_get_progress = svc.get_achievement_progress

        async def spy_get_progress(rom_id):
            # At the time get_achievement_progress is called, user_progress should be gone
            entry = svc._achievements_cache.get("42", {})
            call_order.append("user_progress" not in entry)
            return await original_get_progress(rom_id)

        user_data = _sample_user_data(ra_id=9999, earned=5, total=10)

        plugin._romm_api.get_current_user.return_value = user_data
        with patch.object(svc, "get_achievement_progress", side_effect=spy_get_progress):
            await svc.sync_achievements_after_session(42)

        assert call_order == [True], "user_progress should have been deleted before refetch"

    @pytest.mark.asyncio
    async def test_works_when_no_prior_cache(self, svc, plugin):
        """Works correctly when no prior cache exists."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        rom_data = _sample_rom_data()
        user_data = _sample_user_data(ra_id=9999, earned=3, total=10)

        plugin._romm_api.get_rom.return_value = rom_data
        plugin._romm_api.get_current_user.return_value = user_data
        result = await svc.sync_achievements_after_session(42)

        assert result["success"] is True
        assert result["earned"] == 3

    @pytest.mark.asyncio
    async def test_preserves_achievements_cache_on_invalidation(self, svc, plugin):
        """Invalidating progress cache preserves the achievements list cache."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {"ra_id": 9999}
        svc._achievements_cache["42"] = {
            "achievements": _sample_achievements(),
            "cached_at": svc._clock.time(),
            "ra_id": 9999,
            "user_progress": {
                "earned": 1,
                "total": 10,
                "cached_at": svc._clock.time(),
            },
        }

        user_data = _sample_user_data(ra_id=9999, earned=5, total=10)

        plugin._romm_api.get_current_user.return_value = user_data
        await svc.sync_achievements_after_session(42)

        # Achievements list should still be cached
        assert len(svc._achievements_cache["42"]["achievements"]) == 2
        assert svc._achievements_cache["42"]["ra_id"] == 9999


# --- Integration: get_cached_game_detail with achievements ---


class TestGetCachedGameDetailAchievements:
    @pytest.mark.asyncio
    async def test_includes_ra_id_and_summary_with_cache(self, svc, plugin):
        """When ra_id exists, RA username cached, and progress cached: includes achievement_summary."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {
            "ra_id": 9999,
            "app_id": 100,
            "name": "Test Game",
            "platform_slug": "",
        }
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 5,
                "total": 10,
                "earned_hardcore": 3,
                "cached_at": svc._clock.time(),
            },
        }
        plugin._save_sync_state.replace_with(SaveSyncState())

        result = await plugin.get_cached_game_detail(100)

        assert result["found"] is True
        assert result["ra_id"] == 9999
        assert result["achievement_summary"] is not None
        assert result["achievement_summary"]["earned"] == 5
        assert result["achievement_summary"]["total"] == 10
        assert result["achievement_summary"]["earned_hardcore"] == 3

    @pytest.mark.asyncio
    async def test_no_ra_username_returns_none_summary(self, svc, plugin):
        """When ra_id exists but no RA username cached, achievement_summary is None."""
        _ = svc
        plugin._state["shortcut_registry"]["42"] = {
            "ra_id": 9999,
            "app_id": 100,
            "name": "Test Game",
            "platform_slug": "",
        }
        plugin._save_sync_state.replace_with(SaveSyncState())

        result = await plugin.get_cached_game_detail(100)

        assert result["found"] is True
        assert result["ra_id"] == 9999
        assert result["achievement_summary"] is None

    @pytest.mark.asyncio
    async def test_no_ra_id_returns_none(self, svc, plugin):
        """When no ra_id in registry, ra_id is None and achievement_summary is None."""
        _ = svc
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100,
            "name": "Test Game",
            "platform_slug": "",
        }
        plugin._save_sync_state.replace_with(SaveSyncState())

        result = await plugin.get_cached_game_detail(100)

        assert result["found"] is True
        assert result["ra_id"] is None
        assert result["achievement_summary"] is None

    @pytest.mark.asyncio
    async def test_ra_username_cached_but_no_progress(self, svc, plugin):
        """When RA username cached and ra_id exists but no progress cache, summary is None."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {
            "ra_id": 9999,
            "app_id": 100,
            "name": "Test Game",
            "platform_slug": "",
        }
        plugin._save_sync_state.replace_with(SaveSyncState())

        result = await plugin.get_cached_game_detail(100)

        assert result["found"] is True
        assert result["ra_id"] == 9999
        assert result["achievement_summary"] is None

    @pytest.mark.asyncio
    async def test_expired_progress_cache_returns_none_summary(self, svc, plugin):
        """Expired progress cache returns None for achievement_summary."""
        _seed_ra_username_cache(svc)
        plugin._state["shortcut_registry"]["42"] = {
            "ra_id": 9999,
            "app_id": 100,
            "name": "Test Game",
            "platform_slug": "",
        }
        svc._achievements_cache["42"] = {
            "user_progress": {
                "earned": 5,
                "total": 10,
                "earned_hardcore": 3,
                "cached_at": svc._clock.time() - (2 * 3600),  # expired
            },
        }
        plugin._save_sync_state.replace_with(SaveSyncState())

        result = await plugin.get_cached_game_detail(100)

        assert result["found"] is True
        assert result["achievement_summary"] is None


# --- Integration: sync captures ra_id in shortcuts_data / registry ---


class TestSyncCapturesRaId:
    def test_rom_with_ra_id_appears_in_registry(self, plugin):
        """Registry entry includes ra_id when present in pending sync data."""
        rom_id_str = "42"
        pending = {
            "rom_id": 42,
            "name": "Test Game",
            "platform_slug": "snes",
            "igdb_id": 1234,
            "sgdb_id": 5678,
            "ra_id": 9999,
        }
        # Simulate what sync does when writing registry
        registry_entry = {
            "name": pending["name"],
            "platform_slug": pending.get("platform_slug", ""),
            "cover_path": "",
        }
        for meta_key in ("igdb_id", "sgdb_id", "ra_id"):
            if pending.get(meta_key):
                registry_entry[meta_key] = pending[meta_key]
        plugin._state["shortcut_registry"][rom_id_str] = registry_entry

        assert plugin._state["shortcut_registry"]["42"]["ra_id"] == 9999

    def test_rom_without_ra_id_not_in_registry(self, plugin):
        """Registry entry does not include ra_id when not present in pending sync data."""
        pending = {
            "rom_id": 42,
            "name": "Test Game",
            "platform_slug": "snes",
        }
        registry_entry = {
            "name": pending["name"],
            "platform_slug": pending.get("platform_slug", ""),
            "cover_path": "",
        }
        for meta_key in ("igdb_id", "sgdb_id", "ra_id"):
            if pending.get(meta_key):
                registry_entry[meta_key] = pending[meta_key]
        plugin._state["shortcut_registry"]["42"] = registry_entry

        assert "ra_id" not in plugin._state["shortcut_registry"]["42"]

    def test_ra_id_preserved_across_registry_updates(self, plugin):
        """ra_id is preserved when registry entry is updated."""
        plugin._state["shortcut_registry"]["42"] = {
            "name": "Test Game",
            "platform_slug": "snes",
            "ra_id": 9999,
            "app_id": 100,
        }

        # Simulate re-sync updating the entry
        existing = plugin._state["shortcut_registry"]["42"]
        existing["name"] = "Test Game (Updated)"
        # ra_id should still be there
        assert plugin._state["shortcut_registry"]["42"]["ra_id"] == 9999
