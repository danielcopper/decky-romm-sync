"""Shared factories and fakes for the SaveService test suite."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from conftest import _make_retry
from fakes.fake_save_api import FakeSaveApi
from fakes.system_time import FakeClock

from adapters.persistence import PersistenceAdapter, SaveSyncStatePersisterAdapter
from adapters.save_file import SaveFileAdapter
from services.saves import SaveService, SaveServiceConfig


def _make_save_sync_state_persister(tmp_path) -> SaveSyncStatePersisterAdapter:
    """Adapter rooted at tmp_path so disk-touching tests stay end-to-end."""
    return SaveSyncStatePersisterAdapter(
        PersistenceAdapter(
            settings_dir=str(tmp_path),
            runtime_dir=str(tmp_path),
            logger=logging.getLogger("test"),
        )
    )


_CONFIG_FIELDS = frozenset(
    {
        "runtime_dir",
        "save_sync_state_persister",
        "save_file",
        "loop",
        "logger",
        "clock",
        "get_saves_path",
        "get_roms_path",
        "get_active_core",
        "get_core_name",
        "plugin_version",
        "emit",
        "detect_sort_change",
        "is_retrodeck_migration_pending",
    }
)


def make_service(tmp_path, fake_api=None, *, emit=None, **overrides) -> tuple["SaveService", "FakeSaveApi"]:
    """Create a SaveService with sensible defaults for testing."""
    save_file = SaveFileAdapter()
    fake: FakeSaveApi = fake_api or FakeSaveApi(save_file=save_file)
    # Tests that build their own FakeSaveApi without wiring the adapter get
    # the same instance as the service so download_save_content materializes
    # bytes onto the shared filesystem view.
    if fake.save_file is None:
        fake.save_file = save_file
    config_kwargs: dict[str, Any] = dict(
        runtime_dir=str(tmp_path),
        save_sync_state_persister=_make_save_sync_state_persister(tmp_path),
        save_file=save_file,
        loop=asyncio.get_event_loop(),
        logger=logging.getLogger("test"),
        clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
        get_saves_path=lambda: str(tmp_path / "saves"),
        get_roms_path=lambda: str(tmp_path / "retrodeck" / "roms"),
        get_active_core=lambda system_name, rom_filename=None: (None, None),
        plugin_version="0.14.0",
        emit=emit,
    )
    ctor_kwargs: dict[str, Any] = dict(
        romm_api=fake,
        retry=_make_retry(),
        settings={"log_level": "debug"},
        state={"shortcut_registry": {}, "installed_roms": {}},
        save_sync_state=SaveService.make_default_state(),
    )
    for key, value in overrides.items():
        if key in _CONFIG_FIELDS:
            config_kwargs[key] = value
        else:
            ctor_kwargs[key] = value
    svc = SaveService(**ctor_kwargs, config=SaveServiceConfig(**config_kwargs))
    svc.init_state()
    return svc, fake


def _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba"):
    """Register a ROM in installed_roms state."""
    svc._state["installed_roms"][str(rom_id)] = {
        "rom_id": rom_id,
        "file_name": file_name,
        "file_path": str(tmp_path / "retrodeck" / "roms" / system / file_name),
        "system": system,
        "platform_slug": system,
        "installed_at": "2026-01-01T00:00:00",
    }


def _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 1024, ext=".srm"):
    """Create a save file on disk and return its path."""
    saves_dir = tmp_path / "saves" / system
    saves_dir.mkdir(parents=True, exist_ok=True)
    save_file = saves_dir / (rom_name + ext)
    save_file.write_bytes(content)
    return save_file


_SERVER_SAVE_SENTINEL = object()


def _server_save(
    save_id=100,
    rom_id=42,
    filename="pokemon.srm",
    updated_at="2026-02-17T06:00:00Z",
    file_size_bytes=1024,
    slot=_SERVER_SAVE_SENTINEL,
    file_name_no_tags=None,
):
    if file_name_no_tags is None:
        # Strip extension to approximate RomM's file_name_no_tags
        file_name_no_tags = filename.rsplit(".", 1)[0] if "." in filename else filename
    result = {
        "id": save_id,
        "rom_id": rom_id,
        "file_name": filename,
        "file_name_no_tags": file_name_no_tags,
        "updated_at": updated_at,
        "file_size_bytes": file_size_bytes,
        "emulator": "retroarch",
        "download_path": f"/saves/{filename}",
    }
    if slot is not _SERVER_SAVE_SENTINEL:
        result["slot"] = slot
    return result


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _enable_sync_with_device(svc, device_id: str = "device-1") -> None:
    """Flip on save sync and bind a server device id (matches FakeSaveApi)."""
    svc._save_sync_state.settings.save_sync_enabled = True
    svc._save_sync_state.device_id = device_id
    svc._save_sync_state.server_device_id = device_id


def _server_save_with_syncs(
    *,
    save_id: int = 100,
    rom_id: int = 42,
    filename: str = "pokemon.srm",
    updated_at: str = "2026-02-17T06:00:00Z",
    file_size_bytes: int = 1024,
    device_syncs: list[dict] | None = None,
    slot: str | None = None,
) -> dict:
    """Build a server-save dict with explicit device_syncs (no FakeApi shimming)."""
    base = _server_save(
        save_id=save_id,
        rom_id=rom_id,
        filename=filename,
        updated_at=updated_at,
        file_size_bytes=file_size_bytes,
    )
    if slot is not None:
        base["slot"] = slot
    base["device_syncs"] = device_syncs if device_syncs is not None else []
    return base
