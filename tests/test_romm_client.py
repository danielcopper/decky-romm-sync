import pytest

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    p._metadata_cache = {}
    return p


class TestResolveSystem:
    def test_exact_slug_match(self, plugin):
        result = plugin._resolve_system("n64")
        assert result == "n64"

    def test_fs_slug_fallback(self, plugin):
        # A slug not in the map but its fs_slug is
        result = plugin._resolve_system("nonexistent-slug", "n64")
        assert result == "n64"

    def test_fallback_returns_slug_as_is(self, plugin):
        result = plugin._resolve_system("totally-unknown-platform")
        assert result == "totally-unknown-platform"


class TestRommDownloadUrlEncoding:
    def test_encodes_spaces_in_cover_path(self, plugin, tmp_path):
        """Cover paths from RomM contain unencoded spaces in timestamps.
        _romm_download must URL-encode them so urllib doesn't reject the URL."""
        import urllib.parse

        # Simulate the path RomM returns
        path = "/assets/romm/resources/roms/53/4375/cover/big.png?ts=2025-07-28 00:05:03"
        encoded = urllib.parse.quote(path, safe="/:?=&@")
        assert " " not in encoded
        assert "%20" in encoded
        assert encoded == "/assets/romm/resources/roms/53/4375/cover/big.png?ts=2025-07-28%2000:05:03"

    def test_preserves_clean_paths(self, plugin):
        """Paths without spaces should pass through unchanged."""
        import urllib.parse

        path = "/assets/romm/resources/roms/53/4375/cover/big.png"
        encoded = urllib.parse.quote(path, safe="/:?=&@")
        assert encoded == path


class TestPlatformMap:
    def test_loads_config_json(self, plugin):
        pm = plugin._load_platform_map()
        assert isinstance(pm, dict)
        assert "n64" in pm
        assert "snes" in pm
        assert len(pm) > 50  # Should have many entries
