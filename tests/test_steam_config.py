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


class TestAppIdGeneration:
    def test_generates_signed_int32(self, plugin):
        app_id = plugin._generate_app_id("/path/to/exe", "Test Game")
        assert isinstance(app_id, int)
        assert app_id < 0  # Should be negative (high bit set)

    def test_deterministic(self, plugin):
        id1 = plugin._generate_app_id("/path/exe", "Game")
        id2 = plugin._generate_app_id("/path/exe", "Game")
        assert id1 == id2

    def test_different_names_different_ids(self, plugin):
        id1 = plugin._generate_app_id("/path/exe", "Game A")
        id2 = plugin._generate_app_id("/path/exe", "Game B")
        assert id1 != id2


class TestArtworkIdGeneration:
    def test_generates_unsigned(self, plugin):
        art_id = plugin._generate_artwork_id("/path/exe", "Game")
        assert art_id > 0

    def test_matches_app_id_bits(self, plugin):
        # artwork_id and app_id should share the same CRC base
        art_id = plugin._generate_artwork_id("/path/exe", "Game")
        assert art_id & 0x80000000  # High bit set
