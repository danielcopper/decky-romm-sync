"""Tests for DeviceRegistry — device-registration entry points used by every
save-sync callable when the local device_id is missing. Sync-callable behaviour
on registration failure is exercised here because that surface is what the
DeviceRegistry contract guarantees; pure-orchestration assertions live in
test_engine.py.
"""

import pytest

from lib.errors import RommApiError
from tests.services.saves._helpers import (
    _create_save,
    _install_rom,
    make_service,
)


class TestEnsureDeviceRegisteredFailurePaths:
    """When register_device fails, the four sync callables must surface
    DEVICE_NOT_REGISTERED instead of proceeding with a missing device_id
    (engine.py lines 309-311 / 365 / 407 / 437-439)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
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
        svc._save_sync_state.settings.save_sync_enabled = True
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
        svc._save_sync_state.settings.save_sync_enabled = True
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
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id set.
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.sync_all_saves()

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No per-ROM sync ran.
        assert not any(c[0] == "list_saves" for c in fake.call_log)
