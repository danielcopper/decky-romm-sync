"""Tests for DeviceRegistry — device-registration entry points used by every
save-sync callable when the local device_id is missing. Sync-callable behaviour
on registration failure is exercised here because that surface is what the
DeviceRegistry contract guarantees; pure-orchestration assertions live in
test_engine.py.
"""

import logging

import pytest
from _factories import _make_retry
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_machine_id_reader import FakeMachineIdReader
from fakes.fake_save_api import FakeSaveApi
from fakes.fake_unit_of_work import FakeUnitOfWorkFactory

from domain.playtime import PendingPlaySession, Playtime
from lib.errors import (
    RommApiError,
    RommAuthError,
    RommConnectionError,
    RommForbiddenError,
    RommNotFoundError,
    RommSSLError,
    RommTimeoutError,
)
from lib.list_result import ErrorCode
from services.saves.sync_engine.devices import DeviceRegistry
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _get_device_id,
    _install_rom,
    _seed_rom,
    _uow,
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


class TestEnsureDeviceRegisteredVersionProbe:
    """The pre-registration probe caches SYSTEM.VERSION onto the API adapter, but
    the value is server-controlled: only a real, non-empty version string is
    cached; a truthy non-str (e.g. numeric 4.9) is treated as absent (#1275)."""

    @pytest.mark.asyncio
    async def test_non_string_version_not_cached(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id + no cached version → the pre-register version probe runs.
        fake.heartbeat_payload = {"SYSTEM": {"VERSION": 4.9}}

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        # The truthy non-str version was NOT cached — the adapter stays version-less.
        assert fake.get_version() is None

    @pytest.mark.asyncio
    async def test_string_version_is_cached(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        fake.heartbeat_payload = {"SYSTEM": {"VERSION": "4.9.0"}}

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        # A real version string is cached as before — existing behavior unchanged.
        assert fake.get_version() == "4.9.0"


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


class TestEnsureDeviceRegisteredReRegistersDeadDevice:
    """#1560: after a RomM database wipe/restore the cached device id no longer
    exists server-side, so the best-effort ``update_device`` touch gets a
    definitive 404. That 404 (and ONLY that) forgets the dead id and
    re-registers a fresh one; every other touch failure stays a best-effort
    swallow that keeps the cached id, so a server blip never churns a
    re-registration."""

    @pytest.mark.asyncio
    async def test_live_cached_id_touch_succeeds_no_reregistration(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        # Stamp a version so the pre-register heartbeat probe is skipped — the
        # touch lands on update_device, which succeeds against a live id.
        fake.set_version("4.9.0")

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        # The live touch fired and kept the id — no re-registration, id intact.
        assert any(c[0] == "update_device" for c in fake.call_log)
        assert _register_call(fake) is None
        assert _get_device_id(svc) == "server-uuid"

    @pytest.mark.asyncio
    async def test_dead_cached_id_forgets_and_reregisters(self, tmp_path):
        debug_log: list[str] = []
        svc, fake = make_service(tmp_path, log_debug=debug_log.append)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        # The server no longer has this device → the touch 404s (the #1560 wedge).
        fake.fail_on_next(RommNotFoundError("Device with ID server-uuid not found"))

        result = await svc.ensure_device_registered()

        # A fresh id was minted and persisted, and it is the one returned — not
        # the stale, dead one.
        assert result["success"] is True
        new_id = result["device_id"]
        assert new_id
        assert new_id != "server-uuid"
        # register_device ran (the forget fell through to registration)...
        assert _register_call(fake) is not None
        # ...and the persisted kv_config id is now the fresh id, not the dead one.
        assert _get_device_id(svc) == new_id
        assert result["server_device_id"] == new_id
        # The heal took the 404 branch (naming the dead id), not the generic swallow.
        assert any("server no longer has device server-uuid" in m for m in debug_log)

    @pytest.mark.asyncio
    async def test_transport_failure_on_touch_is_not_a_heal(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        # A transport blip on the touch — NOT a definitive 404. The device is
        # still registered; the id must be kept, not forgotten + re-registered.
        fake.fail_on_next(RommConnectionError("Connection refused"))

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        # The cached id was KEPT — no forget, no re-registration.
        assert _get_device_id(svc) == "server-uuid"
        assert _register_call(fake) is None

    @pytest.mark.asyncio
    async def test_timeout_on_touch_is_not_a_heal(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        fake.fail_on_next(RommTimeoutError("timed out"))

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        assert _get_device_id(svc) == "server-uuid"
        assert _register_call(fake) is None

    @pytest.mark.asyncio
    async def test_generic_exception_on_touch_is_not_a_heal(self, tmp_path):
        """Edge: only a RommNotFoundError heals — a non-RomM error stays a swallow.

        Guards the discriminator: broadening the peel to ``except Exception``
        would re-register on any error, throwing away a valid id.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        fake.fail_on_next(RuntimeError("unexpected"))

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        assert _get_device_id(svc) == "server-uuid"
        assert _register_call(fake) is None

    @pytest.mark.asyncio
    async def test_heal_then_reregister_fails_leaves_clean_state(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")

        # The touch 404s (dead id) → forget → but the server goes away mid-heal,
        # so the re-registration itself fails. The touch is monkeypatched to 404
        # (leaving the one-shot ``fail_on_next`` for the register_device that
        # follows), since a single ``fail_on_next`` can only arm one call.
        def _touch_404(*_args, **_kwargs):
            raise RommNotFoundError("Device with ID server-uuid not found")

        fake.update_device = _touch_404
        fake.fail_on_next(RommConnectionError("Connection refused"))

        result = await svc.ensure_device_registered()

        # Clean, recoverable state: classified failure, empty id, dead id cleared.
        assert result["success"] is False
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert result["message"]
        assert result["device_id"] == ""
        # The dead id was forgotten and NOT replaced — kv_config is left cleared,
        # so the next ensure_device_registered retries cleanly from None.
        assert _get_device_id(svc) is None

    @pytest.mark.asyncio
    async def test_no_cached_id_goes_straight_to_registration(self, tmp_path):
        """First-time path is unchanged: no cached id → no touch, register runs."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        fake.set_version("4.9.0")
        # No device_id persisted → registration branch.

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"]
        # The update_device touch is only for an existing id — it must not run on
        # the first-time path.
        assert not any(c[0] == "update_device" for c in fake.call_log)
        assert _register_call(fake) is not None


class TestDeviceHealReAddressesPlaytimeOutbox:
    """A heal replaces the server registration of the SAME physical device, so the
    play sessions still queued under the dead id are re-addressed to the fresh one
    — otherwise they keep naming an id the server has never heard of."""

    @staticmethod
    def _seed_pending(svc, rom_id: int, device_id: str, *, starts: tuple[str, ...], attempts: int = 0) -> None:
        """Queue outbox rows for *rom_id* addressed to *device_id* (FK parent first)."""
        _seed_rom(svc, rom_id)
        with _uow(svc) as uow:
            uow.playtime.save(
                rom_id,
                Playtime(
                    pending_sessions={
                        start: PendingPlaySession(
                            device_id=device_id,
                            end_time=f"{start}-end",
                            duration_ms=60_000,
                            attempts=attempts,
                        )
                        for start in starts
                    }
                ),
            )

    @staticmethod
    def _pending(svc, rom_id: int) -> dict[str, PendingPlaySession]:
        with _uow(svc) as uow:
            entry = uow.playtime.get(rom_id)
        return {} if entry is None else entry.pending_sessions

    @pytest.mark.asyncio
    async def test_heal_moves_queued_sessions_to_the_fresh_id(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        self._seed_pending(svc, 42, "server-uuid", starts=("2026-01-01T10:00:00",), attempts=3)
        fake.fail_on_next(RommNotFoundError("Device with ID server-uuid not found"))

        result = await svc.ensure_device_registered()

        new_id = result["device_id"]
        assert new_id
        assert new_id != "server-uuid"
        session = self._pending(svc, 42)["2026-01-01T10:00:00"]
        # Re-addressed to the id the server now knows...
        assert session.device_id == new_id
        # ...with the session itself intact — a re-address moves the addressee only.
        assert session.end_time == "2026-01-01T10:00:00-end"
        assert session.duration_ms == 60_000
        # The quarantine ceiling survives, so the outbox keeps its loop-free bound.
        assert session.attempts == 3

    @pytest.mark.asyncio
    async def test_heal_moves_every_queued_row_past_the_flush_batch_limit(self, tmp_path):
        """A backlog larger than one flush batch (100) moves whole — a partial
        re-address would leave the surplus rows stuck on the dead id and crowd
        live sessions out of every future flush window."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        for rom_id in (1, 2, 3):
            self._seed_pending(
                svc,
                rom_id,
                "server-uuid",
                starts=tuple(f"2026-01-01T{hour:02d}:00:00" for hour in range(50)),
            )
        fake.fail_on_next(RommNotFoundError("Device with ID server-uuid not found"))

        result = await svc.ensure_device_registered()

        new_id = result["device_id"]
        moved = [s.device_id for rom_id in (1, 2, 3) for s in self._pending(svc, rom_id).values()]
        assert len(moved) == 150
        assert set(moved) == {new_id}

    @pytest.mark.asyncio
    async def test_rows_queued_on_another_device_are_left_alone(self, tmp_path):
        """Only the dead id is re-addressed — a row naming some other device is
        not this heal's business."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        self._seed_pending(svc, 42, "server-uuid", starts=("s-dead",))
        self._seed_pending(svc, 43, "other-device", starts=("s-other",))
        fake.fail_on_next(RommNotFoundError("Device with ID server-uuid not found"))

        result = await svc.ensure_device_registered()

        assert self._pending(svc, 42)["s-dead"].device_id == result["device_id"]
        assert self._pending(svc, 43)["s-other"].device_id == "other-device"

    @pytest.mark.asyncio
    async def test_live_touch_leaves_the_outbox_untouched(self, tmp_path):
        """No heal, no re-address: the cached id is still the server's id."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        self._seed_pending(svc, 42, "server-uuid", starts=("s1",))

        await svc.ensure_device_registered()

        assert self._pending(svc, 42)["s1"].device_id == "server-uuid"

    @pytest.mark.asyncio
    async def test_failed_reregistration_leaves_sessions_on_the_old_id(self, tmp_path):
        """The re-address is bound to a MINTED id: when registration fails there is
        no new addressee, so the rows stay queued as they were (nothing is lost)."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        fake.set_version("4.9.0")
        self._seed_pending(svc, 42, "server-uuid", starts=("s1",))

        def _touch_404(*_args, **_kwargs):
            raise RommNotFoundError("Device with ID server-uuid not found")

        fake.update_device = _touch_404
        fake.fail_on_next(RommConnectionError("Connection refused"))

        result = await svc.ensure_device_registered()

        assert result["success"] is False
        assert self._pending(svc, 42)["s1"].device_id == "server-uuid"


class TestPermissionDegradedNeverDropsDeviceId:
    """#1437 regression guard: a permission-degraded (403) registration must NEVER
    delete ``kv_config["device_id"]``. The only in-process deleter is
    ``DeviceRegistry.forget_device`` (reached solely from a sign-in origin
    change); registration/verification failures leave the id intact so the next
    sync attributes uploads as before instead of re-registering into a spurious
    conflict."""

    @pytest.mark.asyncio
    async def test_forbidden_update_touch_leaves_the_id_intact(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, "server-uuid")
        # Stamp a version so the pre-register heartbeat probe is skipped — the 403
        # lands on the best-effort update_device touch on the already-registered id.
        fake.set_version("4.9.0")
        fake.fail_on_next(RommForbiddenError("403 Forbidden"))

        result = await svc.ensure_device_registered()

        # The touch failed (non-fatal) but registration still reports the existing id.
        assert result["success"] is True
        assert result["device_id"] == "server-uuid"
        # The persisted row is untouched — no forget/delete on a permission failure.
        assert _get_device_id(svc) == "server-uuid"
        # The id was NOT dropped-then-re-registered: no register_device call happened.
        assert _register_call(fake) is None

    @pytest.mark.asyncio
    async def test_forbidden_registration_creates_no_id_and_deletes_none(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id yet → registration branch; stamp a version to skip the probe
        # so the 403 lands on register_device itself.
        fake.set_version("4.9.0")
        fake.fail_on_next(RommForbiddenError("403 Forbidden"))

        result = await svc.ensure_device_registered()

        assert result["success"] is False
        # No id was minted, and nothing was deleted — kv_config stays absent.
        assert result["device_id"] == ""
        assert _get_device_id(svc) is None


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


class TestForgetDevice:
    """0a (#1234): forget_device drops the kv_config row AND keeps the cache
    coherent, so a server-origin change cannot leave a stale id behind."""

    def test_forget_deletes_the_kv_config_row(self):
        registry, uow_factory, _fake = _make_registry()
        with uow_factory() as uow:
            uow.kv_config.set("device_id", "old-server-uuid")

        registry.forget_device()

        # The row is gone — a fresh, independent read sees nothing.
        with uow_factory() as uow:
            assert uow.kv_config.get("device_id") is None

    def test_forget_serves_none_from_cache_without_re_read(self):
        registry, uow_factory, _fake = _make_registry()
        with uow_factory() as uow:
            uow.kv_config.set("device_id", "old-server-uuid")
        registry.get_device_id()  # caches "old-server-uuid"

        registry.forget_device()
        opens_after_forget = uow_factory.call_count

        # The cache is coherent: the id now reads absent with no extra UoW open.
        assert registry.get_device_id() is None
        assert uow_factory.call_count == opens_after_forget

    def test_forget_when_unregistered_is_a_noop(self):
        """First-sign-in / never-registered: forgetting an absent id is harmless."""
        registry, _uow_factory, _fake = _make_registry()
        registry.forget_device()
        assert registry.get_device_id() is None
