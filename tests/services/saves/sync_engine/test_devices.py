"""Tests for DeviceRegistry — device-registration entry points used by every
save-sync callable when the local device_id is missing. Sync-callable behaviour
on registration failure is exercised here because that surface is what the
DeviceRegistry contract guarantees; pure-orchestration assertions live in
test_engine.py.
"""

import logging

import pytest
from conftest import _make_retry
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_machine_id_reader import FakeMachineIdReader
from fakes.fake_save_api import FakeSaveApi
from fakes.fake_unit_of_work import FakeUnitOfWorkFactory

from lib.errors import RommApiError, RommAuthError, RommConnectionError, RommSSLError
from lib.list_result import ErrorCode
from services.saves.sync_engine.devices import DeviceRegistry
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _install_rom,
    make_service,
)


class _FailingSettingsPersister:
    """A ``SettingsPersister`` whose ``save_settings`` always raises.

    Used to exercise the best-effort device-name write: a settings.json
    write failure during registration must leave the device usable, not in a
    broken half-state.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.save_count = 0

    def save_settings(self) -> None:
        self.save_count += 1
        raise self._exc


def _make_registry(*, settings=None, settings_persister=None):
    """Build a stand-alone :class:`DeviceRegistry` over fresh fakes.

    Returns ``(registry, uow_factory, fake_api)`` so a test can drive the
    registry in isolation and count the underlying Unit-of-Work opens.
    """
    uow_factory = FakeUnitOfWorkFactory()
    fake = FakeSaveApi()
    registry = DeviceRegistry(
        uow_factory=uow_factory,
        settings=settings if settings is not None else {"save_sync_enabled": True},
        romm_api=fake,
        retry=_make_retry(),
        logger=logging.getLogger("test"),
        log_debug=lambda msg: None,
        settings_persister=settings_persister or _FailingSettingsPersister(RuntimeError("unused")),
        plugin_version="0.14.0",
    )
    return registry, uow_factory, fake


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


class TestRegistrationDeviceNameWriteIsBestEffort:
    """The kv_config device id is the AUTHORITATIVE registered signal — written
    first. The settings.json device_name is a best-effort write AFTER, so a
    label-write failure leaves a fully registered, usable device (valid id,
    prior/default name) instead of a broken half-state (#984)."""

    @pytest.mark.asyncio
    async def test_device_id_persisted_when_name_write_fails(self, tmp_path):
        debug_log: list[str] = []
        svc, _fake = make_service(
            tmp_path,
            log_debug=debug_log.append,
            settings_persister=_FailingSettingsPersister(OSError("settings.json fsync failed")),
        )
        svc._config.settings["save_sync_enabled"] = True
        # No prior device_name — the failed write leaves it unset.
        svc._config.settings.pop("device_name", None)

        result = await svc.ensure_device_registered()

        # The device is fully registered and usable despite the name-write failure.
        assert result["success"] is True
        assert result["device_id"]  # a real server id was issued and persisted
        # The id is the authoritative registered signal — written before the name.
        assert svc._sync_engine.get_device_id() == result["device_id"]
        # The label write failed, so the device falls back to the prior/default
        # name (empty here) rather than the half-applied hostname.
        assert result["device_name"] == ""
        # The swallow leaves a debug breadcrumb naming the still-usable id.
        assert any(
            "device_name write failed" in m and result["device_id"] in m and "fsync failed" in m for m in debug_log
        )

    @pytest.mark.asyncio
    async def test_prior_device_name_survives_a_failed_name_write(self, tmp_path):
        svc, _fake = make_service(
            tmp_path,
            settings_persister=_FailingSettingsPersister(OSError("disk full")),
        )
        svc._config.settings["save_sync_enabled"] = True
        svc._config.settings["device_name"] = "previous-label"

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"]
        # The previously-persisted label is preserved as the fallback.
        assert result["device_name"] == "previous-label"


class TestDeviceIdCachedRead:
    """DeviceRegistry is the single device-id owner: it reads kv_config ONCE and
    serves the cached value thereafter, never re-querying per call (#984)."""

    def test_repeated_reads_open_one_unit_of_work(self):
        registry, uow_factory, _fake = _make_registry()
        with uow_factory() as uow:
            uow.kv_config.set("device_id", "server-uuid")
        opens_after_seed = uow_factory.call_count

        first = registry.get_device_id()
        second = registry.get_device_id()
        third = registry.get_device_id()

        assert first == second == third == "server-uuid"
        # Exactly one additional UoW open across the three reads — the cache
        # served the 2nd and 3rd without re-querying SQLite.
        assert uow_factory.call_count == opens_after_seed + 1

    def test_invalidate_forces_a_re_read(self):
        registry, uow_factory, _fake = _make_registry()
        with uow_factory() as uow:
            uow.kv_config.set("device_id", "first")
        registry.get_device_id()  # caches "first"

        # Mutate kv_config behind the registry's back, then invalidate.
        with uow_factory() as uow:
            uow.kv_config.set("device_id", "second")
        opens_before = uow_factory.call_count
        registry.invalidate_device_id_cache()

        assert registry.get_device_id() == "second"
        # The invalidation forced exactly one fresh read.
        assert uow_factory.call_count == opens_before + 1


class TestDeviceIdAbsent:
    """Edge: no device registered yet — get_device_id behaves as before (None),
    and a cached absent result is not re-queried on every call (#984)."""

    def test_unregistered_returns_none(self):
        registry, _uow_factory, _fake = _make_registry()
        assert registry.get_device_id() is None

    def test_absent_result_is_cached(self):
        registry, uow_factory, _fake = _make_registry()
        assert registry.get_device_id() is None
        opens_after_first = uow_factory.call_count

        # A second read of the still-unregistered device must not re-query —
        # "read and found absent" is distinct from "never read".
        assert registry.get_device_id() is None
        assert uow_factory.call_count == opens_after_first
