"""Tests for DeviceRegistry — device-registration entry points used by every
save-sync callable when the local device_id is missing. Sync-callable behaviour
on registration failure is exercised here because that surface is what the
DeviceRegistry contract guarantees; pure-orchestration assertions live in
test_engine.py.
"""

import pytest
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_machine_id_reader import FakeMachineIdReader

from lib.errors import RommApiError
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _install_rom,
    make_service,
)


def _register_call(fake):
    """Return the recorded ``register_device`` call entry, or ``None``."""
    for entry in fake.call_log:
        if entry[0] == "register_device":
            return entry
    return None


class TestEnsureDeviceRegisteredFingerprint:
    """The machine-id is sent as the RomM ``hostname`` fingerprint so the
    server dedupes this device across reinstalls; the friendly OS hostname
    is the display ``name`` only and must never leak into the fingerprint."""

    @pytest.mark.asyncio
    async def test_register_sends_machine_id_as_hostname(self, tmp_path):
        svc, fake = make_service(
            tmp_path,
            hostname_provider=FakeHostnameReader(hostname="steamdeck"),
            machine_id_provider=FakeMachineIdReader(machine_id="machine-abc-123"),
        )
        svc._config.settings["save_sync_enabled"] = True
        # No device_id persisted → registration branch.

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        entry = _register_call(fake)
        assert entry is not None
        name, _platform, _client, _version = entry[1]
        # Friendly OS hostname is the display name only.
        assert name == "steamdeck"
        # Machine-id is the fingerprint hostname — NOT the OS hostname.
        assert entry[2]["hostname"] == "machine-abc-123"

    @pytest.mark.asyncio
    async def test_register_omits_hostname_when_machine_id_none(self, tmp_path):
        svc, fake = make_service(
            tmp_path,
            hostname_provider=FakeHostnameReader(hostname="steamdeck"),
            machine_id_provider=FakeMachineIdReader(machine_id=None),
        )
        svc._config.settings["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        entry = _register_call(fake)
        assert entry is not None
        name, _platform, _client, _version = entry[1]
        # Degrades to no-fingerprint — hostname is None, never the OS hostname.
        assert entry[2]["hostname"] is None
        assert name != entry[2]["hostname"]

    @pytest.mark.asyncio
    async def test_existing_device_id_skips_registration(self, tmp_path):
        svc, fake = make_service(
            tmp_path,
            machine_id_provider=FakeMachineIdReader(machine_id="machine-abc-123"),
        )
        _enable_sync_with_device(svc, "server-uuid")

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        # Already registered → no register_device call, machine-id unused.
        assert _register_call(fake) is None


class TestEnsureDeviceRegisteredFailurePaths:
    """When register_device fails, the four sync callables must surface
    DEVICE_NOT_REGISTERED instead of proceeding with a missing device_id
    (engine.py lines 309-311 / 365 / 407 / 437-439)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id set — triggers ensure_device_registered.
        _install_rom(svc, tmp_path)
        # register_device raises → ensure_device_registered returns success=False.
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No sync ran — the guard returned early.
        assert not any(c[0] == "list_saves" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id set.
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No upload ran.
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_rom_saves_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id set.
        _install_rom(svc, tmp_path)
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        assert not any(c[0] == "list_saves" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_all_saves_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id set.
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.sync_all_saves()

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No per-ROM sync ran.
        assert not any(c[0] == "list_saves" for c in fake.call_log)
