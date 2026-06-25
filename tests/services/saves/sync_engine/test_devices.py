"""Tests for DeviceRegistry — device-registration entry points used by every
save-sync callable when the local device_id is missing. Sync-callable behaviour
on registration failure is exercised here because that surface is what the
DeviceRegistry contract guarantees; pure-orchestration assertions live in
test_engine.py.
"""

import pytest
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_machine_id_reader import FakeMachineIdReader

from lib.errors import RommApiError, RommAuthError, RommConnectionError, RommSSLError
from lib.list_result import ErrorCode
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


class TestEnsureDeviceRegisteredErrorClassification:
    """When register_device raises, the returned dict carries the CLASSIFIED
    reason + message (auth/SSL get their own slug) instead of every failure
    collapsing onto a generic SERVER_UNREACHABLE "Could not register device"
    (#971)."""

    @pytest.mark.asyncio
    async def test_auth_failure_classifies_to_auth_failed(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # Stamp a version so the pre-register heartbeat probe is skipped — the
        # failure must land on register_device, not the non-fatal version probe.
        fake.set_version("4.8.1")
        # No device_id set → registration branch.
        fake.fail_on_next(RommAuthError("401 Unauthorized"))

        result = await svc.ensure_device_registered()

        assert result["success"] is False
        assert result["reason"] == ErrorCode.AUTH_FAILED.value
        assert "uthentication failed" in result["message"]
        assert result["message"] != "Could not register device"

    @pytest.mark.asyncio
    async def test_ssl_failure_classifies_with_ssl_message(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        fake.set_version("4.8.1")
        fake.fail_on_next(RommSSLError("cert verify failed"))

        result = await svc.ensure_device_registered()

        assert result["success"] is False
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert "SSL" in result["message"]

    @pytest.mark.asyncio
    async def test_connection_failure_classifies_to_unreachable(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        fake.set_version("4.8.1")
        fake.fail_on_next(RommConnectionError("Connection refused"))

        result = await svc.ensure_device_registered()

        assert result["success"] is False
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert "unreachable" in result["message"].lower()


class TestEnsureDeviceRegisteredUpdateSwallowLogs:
    """The best-effort update_device touch on an already-registered device is a
    non-fatal swallow, but it logs at debug so it leaves a breadcrumb (#971)."""

    @pytest.mark.asyncio
    async def test_update_device_failure_logs_at_debug_and_still_succeeds(self, tmp_path):
        debug_log: list[str] = []
        svc, fake = make_service(tmp_path, log_debug=debug_log.append)
        _enable_sync_with_device(svc, "server-uuid")
        # Stamp a version so the pre-register heartbeat probe is skipped — the
        # injected failure must land on the best-effort update_device touch.
        fake.set_version("4.8.1")
        fake.fail_on_next(RommConnectionError("boom"))

        result = await svc.ensure_device_registered()

        # The touch failed but registration still reports success.
        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        assert any("update_device failed" in m and "boom" in m for m in debug_log)
