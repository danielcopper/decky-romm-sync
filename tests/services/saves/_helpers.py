"""Shared factories and fakes for the SaveService test suite."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from conftest import _make_retry
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_machine_id_reader import FakeMachineIdReader
from fakes.fake_plugin_metadata_reader import FakePluginMetadataReader
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_save_api import FakeSaveApi
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from adapters.save_file import SaveFileAdapter
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_save_state import FileSyncState, RomSaveState
from services.saves import SaveService, SaveServiceConfig


async def _noop_emit(_event: str, /, *_args: object) -> None:
    """Default emitter for SaveService tests — drops all events."""


def make_service(tmp_path, fake_api=None, *, emit=None, **overrides) -> tuple["SaveService", "FakeSaveApi"]:
    """Create a SaveService with sensible defaults for testing."""
    save_file_store = SaveFileAdapter()
    fake: FakeSaveApi = fake_api or FakeSaveApi(save_file_store=save_file_store)
    # Tests that build their own FakeSaveApi without wiring the adapter get
    # the same instance as the service so download_save_content materializes
    # bytes onto the shared filesystem view.
    if fake.save_file_store is None:
        fake.save_file_store = save_file_store
    config_kwargs: dict[str, Any] = {
        "romm_api": fake,
        "retry": _make_retry(),
        "settings": {"log_level": "debug"},
        "settings_persister": FakeSettingsPersister(),
        "save_file_store": save_file_store,
        "loop": asyncio.get_event_loop(),
        "logger": logging.getLogger("test"),
        "clock": FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
        "retrodeck_paths": FakeRetroDeckPaths(
            saves=str(tmp_path / "saves"),
            roms=str(tmp_path / "retrodeck" / "roms"),
        ),
        "active_core": FakeActiveCoreResolver(default=(None, None)),
        "hostname_provider": FakeHostnameReader(),
        "machine_id_provider": FakeMachineIdReader(),
        "log_debug": lambda _msg: None,
        "plugin_metadata": FakePluginMetadataReader(version="0.14.0"),
        "plugin_dir": str(tmp_path / "plugin"),
        "emit": emit if emit is not None else _noop_emit,
        "get_core_name": lambda core_so: None,
        "detect_sort_change": lambda: None,
        "is_retrodeck_migration_pending": lambda: False,
        "uow_factory": FakeUnitOfWorkFactory(),
    }
    config_kwargs.update(overrides)
    svc = SaveService(config=SaveServiceConfig(**config_kwargs))
    return svc, fake


def _uow(svc) -> FakeUnitOfWork:
    """Return the shared in-memory unit of work backing *svc*."""
    return svc._uow_factory.uow


def _seed_rom(svc, rom_id: int, *, platform_slug: str = "gba") -> None:
    """Seed a ``Rom`` registry row so per-rom child writes pass the commit-time FK."""
    with _uow(svc) as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=rom_id,
                synced_at="2026-01-01T00:00:00",
            )
        )


def _seed_save_state(svc, rom_id: int, state: RomSaveState, *, platform_slug: str = "gba") -> None:
    """Seed a ``RomSaveState`` aggregate for *rom_id*, seeding its ``Rom`` FK first."""
    _seed_rom(svc, rom_id, platform_slug=platform_slug)
    with _uow(svc) as uow:
        uow.rom_save_states.save(rom_id, state)


def _get_save_state(svc, rom_id: int) -> RomSaveState | None:
    """Read back the persisted ``RomSaveState`` for *rom_id*, or ``None``."""
    with _uow(svc) as uow:
        return uow.rom_save_states.get(rom_id)


def _require_save_state(svc, rom_id: int) -> RomSaveState:
    """Read back the persisted ``RomSaveState`` for *rom_id*, asserting it exists.

    Member-access narrowing twin of :func:`_get_save_state` for the common
    case where a test has just seeded/run a flow and the state is known to be
    present — keeps the call site free of a per-line ``assert ... is not None``.
    """
    state = _get_save_state(svc, rom_id)
    assert state is not None
    return state


def rom_save_state_from_dict(data: dict[str, Any]) -> RomSaveState:
    """Build a ``RomSaveState`` from the legacy dict shape used across saves tests.

    The SQLite aggregate has no ``from_dict``; this test helper preserves the
    ergonomic ``{"files": {fn: {...}}, "active_slot": ...}`` literal the old
    JSON aggregate accepted so the large status/versions/matrix suites migrate
    by import-swap rather than per-literal rewrites.
    """
    _FILE_FIELDS = {
        "tracked_save_id",
        "last_sync_hash",
        "last_sync_at",
        "last_sync_server_updated_at",
        "last_sync_server_save_id",
        "last_sync_server_size",
        "last_sync_local_mtime",
        "last_sync_local_size",
    }
    raw_files = data.get("files", {})
    files = {fn: FileSyncState(**{k: v for k, v in fs.items() if k in _FILE_FIELDS}) for fn, fs in raw_files.items()}
    return RomSaveState(
        active_slot=data.get("active_slot"),
        slot_confirmed=bool(data.get("slot_confirmed", False)),
        emulator=str(data.get("emulator", "retroarch")),
        system=str(data.get("system", "") or ""),
        last_synced_core=data.get("last_synced_core"),
        own_upload_ids=data.get("own_upload_ids"),
        slots=data.get("slots", {}),
        files=files,
        last_sync_check_at=data.get("last_sync_check_at"),
    )


def _seed_save_state_dict(svc, rom_id: int, data: dict[str, Any], *, platform_slug: str = "gba") -> None:
    """Seed a ``RomSaveState`` from the legacy dict shape (seeds the ``Rom`` FK)."""
    _seed_save_state(svc, rom_id, rom_save_state_from_dict(data), platform_slug=platform_slug)


def _do_sync(svc, rom_id: int):
    """Run the matrix sync worker for *rom_id* the way the op-entry does.

    Loads the ``RomSaveState`` + device id from the shared UoW, resolves the
    active core, runs ``do_sync_rom_saves`` (the matrix worker), persists the
    mutated aggregate, and returns ``(synced, errors, conflicts)``. The
    direct-worker tests use this in place of the old ``do_sync_rom_saves(rom_id)``
    call that reached into a global state dict.
    """
    engine = svc._sync_engine
    if engine._rom_info.get_rom_save_info(rom_id) is None:
        # Not installed → nothing to sync and no roms row to anchor a write.
        return engine.do_sync_rom_saves(rom_id, RomSaveState(), None, None, _default_slot(svc))
    save_state, device_id = engine._read_sync_inputs(rom_id)
    core_so = engine.resolve_core(rom_id)
    default_slot = _default_slot(svc)
    result = engine.do_sync_rom_saves(rom_id, save_state, device_id, core_so, default_slot)
    engine._write_save_state(rom_id, save_state)
    return result


def _default_slot(svc):
    """Resolve the configured default slot from the service's settings."""
    from services.saves._settings import resolve_default_slot

    return resolve_default_slot(svc._config.settings)


def _do_upload(svc, rom_id, file_path, filename, system, *, server_save=None, core_so=None):
    """Run the upload worker the way the op-entry does and return the mutated state.

    Loads the ``RomSaveState`` + device id from the shared UoW, calls
    ``do_upload_save`` (which mutates the aggregate in memory), persists it, and
    returns the in-memory state so the test can assert on the same object.
    """
    engine = svc._sync_engine
    save_state, device_id = engine._read_sync_inputs(rom_id)
    engine.do_upload_save(
        rom_id, file_path, filename, save_state, device_id, system, core_so, server_save, _default_slot(svc)
    )
    engine._write_save_state(rom_id, save_state)
    return save_state


def _do_download(svc, server_save, saves_dir, filename, rom_id, system):
    """Run the download worker the way the op-entry does and return the mutated state."""
    engine = svc._sync_engine
    save_state, device_id = engine._read_sync_inputs(rom_id)
    engine.do_download_save(server_save, saves_dir, filename, save_state, device_id, system, _default_slot(svc))
    engine._write_save_state(rom_id, save_state)
    return save_state


def _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba"):
    """Register a ROM install in the ``rom_installs`` aggregate (WS3).

    Seeds the ``Rom`` registry row first so the per-rom child write passes the
    commit-time foreign key the FakeUnitOfWork enforces.
    """
    _seed_rom(svc, rom_id, platform_slug=system)
    with _uow(svc) as uow:
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=str(tmp_path / "retrodeck" / "roms" / system / file_name),
                rom_dir=None,
                platform_slug=system,
                system=system,
                installed_at="2026-01-01T00:00:00",
            )
        )


def _seed_install(
    svc, rom_id: int, *, file_path: str, system: str, platform_slug: str, rom_dir: str | None = None, allow_empty=False
) -> None:
    """Seed a ``RomInstall`` with arbitrary fields (for path/system edge-case tests).

    Seeds the ``Rom`` FK first. Builds the aggregate directly (not via
    ``mark_installed``) when *allow_empty* is set so empty system/path edge cases
    can be exercised.
    """
    _seed_rom(svc, rom_id, platform_slug=platform_slug or "unknown")
    install = RomInstall(
        rom_id=rom_id,
        file_path=file_path,
        rom_dir=rom_dir,
        platform_slug=platform_slug,
        system=system,
        installed_at="2026-01-01T00:00:00",
    )
    with _uow(svc) as uow:
        uow.rom_installs.save(install)


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
    svc._config.settings["save_sync_enabled"] = True
    with _uow(svc) as uow:
        uow.kv_config.set("device_id", device_id)


def _set_device_id(svc, device_id: str | None) -> None:
    """Set or clear the server device id in ``kv_config`` (None deletes it)."""
    with _uow(svc) as uow:
        if device_id is None:
            uow.kv_config.delete("device_id")
        else:
            uow.kv_config.set("device_id", device_id)


def _get_device_id(svc) -> str | None:
    """Read the persisted server device id from ``kv_config``."""
    with _uow(svc) as uow:
        return uow.kv_config.get("device_id")


def _set_sort_settings(svc, settings: dict[str, Any]) -> None:
    """Seed the last-seen save-sort observation marker in ``kv_config``."""
    import json

    with _uow(svc) as uow:
        uow.kv_config.set("save_sort_settings", json.dumps(settings))


def _set_sort_settings_previous(svc, settings: dict[str, Any]) -> None:
    """Seed the pending pre-change save-sort marker in ``kv_config``."""
    import json

    with _uow(svc) as uow:
        uow.kv_config.set("save_sort_settings_previous", json.dumps(settings))


def _server_save_with_syncs(
    *,
    save_id: int = 100,
    rom_id: int = 42,
    filename: str = "pokemon.srm",
    updated_at: str = "2026-02-17T06:00:00Z",
    file_size_bytes: int = 1024,
    device_syncs: list[dict[str, Any]] | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
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
