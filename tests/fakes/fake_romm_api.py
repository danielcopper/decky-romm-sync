"""In-memory ``RommApi`` implementation covering every RomM Protocol surface.

Use this fake anywhere a service needs a RomM transport in tests. It
implements every method declared on the per-domain Protocols in
``services.protocols.transport`` (``RommLibraryApi``, ``RommSyncApi``,
``RommConnectionApi``, ``RommAchievementsApi``, ``RommFirmwareApi``,
``RommPlaytimeApi``, ``RommDeviceApi``, ``RommPlatformReader``,
``RommRomReader``, ``RommSaveApi``, ``RommTokenApi``, ``RommVersion``) so a
single instance satisfies any of them via duck typing.

Seed in-memory state directly on the public attributes
(``platforms`` / ``roms`` / ``firmware_files`` / ``collections`` /
``virtual_collections`` / ``smart_collections`` / ``play_sessions`` / ``saves`` /
``devices``); construct without arguments for tests that only care that
the surface is callable.

Failure injection mirrors ``FakeSaveApi``:

- ``fail_on_next(exc)`` — the next call to **any** method raises and the
  arming is consumed (one-shot).
- ``<method>_side_effect`` attributes — per-method exceptions that fire
  on every call until cleared. Tests reach for these when a specific
  method must fail repeatedly (e.g. heartbeat outages).

Downloads write a deterministic payload to ``dest_path`` via ``pathlib``
so callers that subsequently read the file see real bytes; tests can
stage richer payloads on ``download_payloads`` (``{key -> bytes}``).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fakes._romm_save_semantics import check_add_save_conflict, compute_is_current, tag_filename
from lib.errors import RommUnprocessableEntityError

if TYPE_CHECKING:
    from models.play_sessions import (
        PlaySessionIngestEntry,
        PlaySessionIngestResponse,
        PlaySessionIngestResult,
    )
    from models.sync import (
        ClientSaveState,
        SyncCompleteResponse,
        SyncNegotiateResponse,
    )


class FakeRommApi:
    """In-memory fake that satisfies every RomM Protocol surface without HTTP.

    All RomM Protocols from ``services.protocols.transport`` are implemented
    on this single class so a test can pass one instance wherever a more
    specific Protocol is expected.
    """

    def __init__(self) -> None:
        # In-memory seeded data — tests mutate these directly.
        self.platforms: list[dict[str, Any]] = []
        self.roms: dict[int, dict[str, Any]] = {}
        self.firmware_files: list[dict[str, Any]] = []
        self.collections: list[dict[str, Any]] = []
        self.virtual_collections: dict[str, list[dict[str, Any]]] = {}
        self.smart_collections: list[dict[str, Any]] = []
        # Native play-session store (ADR-0018): rom_id -> stored session dicts.
        # Seed history directly for reconcile tests; ``ingest_play_sessions``
        # appends here and dedupes on ``(device_id, rom_id, start_time)``.
        self.play_sessions: dict[int, list[dict[str, Any]]] = {}
        self._play_session_ledger: set[tuple[str, int, str]] = set()
        # Ingest rejects any submitted session shorter than this (ms) with a
        # ``skipped`` verdict — models RomM refusing a sub-second launch-death.
        # Default 0 never rejects (durations are >= 0), so existing tests stand.
        self.reject_below_duration_ms: int = 0
        # When > 0, ingest models RomM's ATOMIC batch validation: if ANY submitted
        # entry's duration_ms is below this floor, the WHOLE POST is rejected with
        # HTTP 422 (``RommUnprocessableEntityError``) naming every sub-floor index
        # in ``detail[].loc``, and nothing is stored — mirroring #1312's
        # whole-request poison. Distinct from ``reject_below_duration_ms`` (a
        # per-entry ``skipped`` verdict inside a 2xx response).
        self.reject_batch_below_duration_ms: int = 0
        # When True, a MULTI-entry 422 carries ``detail=None`` (a proxy/Cloudflare
        # Tunnel mangled the validation body), while a SINGLE-entry 422 still names
        # its index — modelling #1312's L2 no-usable-index path where the batch
        # gives no target but the per-session re-POST does. Requires
        # ``reject_batch_below_duration_ms > 0`` to fire.
        self.mangle_batch_422_detail: bool = False
        self.saves: dict[int, dict[str, Any]] = {}
        self.devices: list[dict[str, Any]] = []
        self.current_user: dict[str, Any] = {"id": 1, "username": "tester"}
        self.heartbeat_response: dict[str, Any] = {"status": "ok"}
        self._version: str | None = None
        self._save_content: dict[int, bytes] = {}
        # DeviceSaveSync rows: (device_id, save_id) -> last_synced_at (ISO). The
        # server writes one on confirm/optimistic-download; list_saves computes
        # each save's ``device_syncs`` (and is_current) from it, and the
        # add_save 409 gate reads it. Seed directly via ``stage_device_sync``.
        self._device_sync_ledger: dict[tuple[str, int], str] = {}

        # Pagination configurable per ROM listing endpoint. Tests can
        # tweak ``items_per_platform`` keyed by ``(platform_id,)``,
        # ``items_per_collection`` keyed by ``(collection_id,)`` and
        # ``items_per_virtual_collection`` keyed by ``(virtual_id,)``
        # to drive multi-page sync flows.

        # Downloads: optional staged payload bytes for files the test
        # wants to inspect after download.
        # Keys: ``"rom:{rom_id}:{filename}"`` for ROM content,
        # ``"firmware:{firmware_id}:{filename}"`` for firmware,
        # ``"cover:{cover_url}"`` for covers,
        # ``"save:{save_id}"`` for save content.
        self.download_payloads: dict[str, bytes] = {}

        # Failure-injection seams.
        self._fail_on_next: Exception | None = None
        self.heartbeat_side_effect: Exception | None = None
        self.list_platforms_side_effect: Exception | None = None
        self.list_firmware_side_effect: Exception | None = None
        self.get_firmware_side_effect: Exception | None = None
        self.download_firmware_side_effect: Exception | None = None
        self.get_rom_side_effect: Exception | None = None
        self.list_roms_side_effect: Exception | None = None
        self.list_roms_updated_after_side_effect: Exception | None = None
        self.list_collections_side_effect: Exception | None = None
        self.list_virtual_collections_side_effect: Exception | None = None
        self.list_smart_collections_side_effect: Exception | None = None
        self.list_roms_by_collection_side_effect: Exception | None = None
        self.list_roms_by_virtual_collection_side_effect: Exception | None = None
        self.list_roms_by_smart_collection_side_effect: Exception | None = None
        self.download_rom_content_side_effect: Exception | None = None
        # Resumability verdict the fake reports via ``on_meta`` (server range support).
        self.download_range_supported: bool = False
        self.download_cover_side_effect: Exception | None = None
        self.get_current_user_side_effect: Exception | None = None
        self.ingest_play_sessions_side_effect: Exception | None = None
        self.list_play_sessions_side_effect: Exception | None = None
        self.register_device_side_effect: Exception | None = None
        self.list_devices_side_effect: Exception | None = None
        self.update_device_side_effect: Exception | None = None
        self.list_saves_side_effect: Exception | None = None
        self.upload_save_side_effect: Exception | None = None
        self.download_save_side_effect: Exception | None = None
        self.download_save_content_side_effect: Exception | None = None
        self.confirm_download_side_effect: Exception | None = None
        self.get_save_summary_side_effect: Exception | None = None
        self.delete_server_saves_side_effect: Exception | None = None
        self.mint_client_token_side_effect: Exception | None = None
        self.delete_client_token_side_effect: Exception | None = None
        self.exchange_pairing_code_side_effect: Exception | None = None

        # Client-token mint: tests stage the response the next mint returns.
        self.mint_client_token_response: dict[str, Any] = {"id": 1, "raw_token": "rmm_faketoken"}
        # Pairing-code exchange: tests stage the token schema the next exchange returns.
        self.exchange_pairing_code_response: dict[str, Any] = {"id": 2, "raw_token": "rmm_paired"}
        # Deleted token ids, in call order.
        self.deleted_token_ids: list[int] = []

        # Observability — every method records ``(name, args, kwargs)``.
        self.call_log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        # Internal id counters for synthesised entities.
        self._next_save_id = 1000
        self._next_play_session_id = 3000
        self._next_device_id = 1

    # ------------------------------------------------------------------
    # Failure-injection helpers
    # ------------------------------------------------------------------

    def fail_on_next(self, exc: Exception) -> None:
        """Arm the next call (any method) to raise ``exc`` then clear the arming."""
        self._fail_on_next = exc

    def _check_fail(self, method_side_effect: Exception | None = None) -> None:
        """Raise the one-shot ``fail_on_next`` exception, then the per-method one.

        Order is intentional: ``fail_on_next`` is one-shot so it must
        consume first; per-method side effects persist until cleared
        and fire on every call.
        """
        if self._fail_on_next is not None:
            exc = self._fail_on_next
            self._fail_on_next = None
            raise exc
        if method_side_effect is not None:
            raise method_side_effect

    def _log(self, name: str, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> None:
        self.call_log.append((name, args, kwargs or {}))

    def _materialize_download(self, dest_path: str, payload: bytes) -> None:
        """Write ``payload`` bytes to ``dest_path`` via ``pathlib``.

        Mirrors the real adapter's contract that the file exists at
        ``dest_path`` after a successful download. Parent directories
        are created so callers don't need to stage them.
        """
        dest = pathlib.Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)

    # ------------------------------------------------------------------
    # RommVersion
    # ------------------------------------------------------------------

    def set_version(self, version: str | None) -> None:
        self._log("set_version", (version,))
        self._version = version

    def get_version(self) -> str | None:
        self._log("get_version")
        return self._version

    def heartbeat(self) -> dict[str, Any]:
        self._log("heartbeat")
        self._check_fail(self.heartbeat_side_effect)
        return dict(self.heartbeat_response)

    def heartbeat_once(self) -> dict[str, Any]:
        # Single-attempt reachability probe — same response/side-effect as the
        # retrying heartbeat (the fake has no retry to bypass).
        self._log("heartbeat_once")
        self._check_fail(self.heartbeat_side_effect)
        return dict(self.heartbeat_response)

    def get_current_user(self) -> dict[str, Any]:
        self._log("get_current_user")
        self._check_fail(self.get_current_user_side_effect)
        return dict(self.current_user)

    # ------------------------------------------------------------------
    # RommPlatformReader
    # ------------------------------------------------------------------

    def list_platforms(self) -> list[dict[str, Any]]:
        self._log("list_platforms")
        self._check_fail(self.list_platforms_side_effect)
        return [dict(p) for p in self.platforms]

    # ------------------------------------------------------------------
    # RommRomReader
    # ------------------------------------------------------------------

    def get_rom(self, rom_id: int) -> dict[str, Any]:
        self._log("get_rom", (rom_id,))
        self._check_fail(self.get_rom_side_effect)
        rom = self.roms.get(rom_id)
        if rom is None:
            return {"id": rom_id}
        return dict(rom)

    def _paginate(self, items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
        sliced = items[offset : offset + limit]
        return {"items": [dict(r) for r in sliced], "total": len(items)}

    def list_roms(self, platform_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self._log("list_roms", (platform_id,), {"limit": limit, "offset": offset})
        self._check_fail(self.list_roms_side_effect)
        items = [r for r in self.roms.values() if r.get("platform_id") == platform_id]
        return self._paginate(items, limit, offset)

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._log(
            "list_roms_updated_after",
            (platform_id, updated_after),
            {"limit": limit, "offset": offset},
        )
        self._check_fail(self.list_roms_updated_after_side_effect)
        items = [
            r
            for r in self.roms.values()
            if r.get("platform_id") == platform_id and (r.get("updated_at") or "") > updated_after
        ]
        return self._paginate(items, limit, offset)

    def list_roms_by_collection(self, collection_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self._log(
            "list_roms_by_collection",
            (collection_id,),
            {"limit": limit, "offset": offset},
        )
        self._check_fail(self.list_roms_by_collection_side_effect)
        items = [r for r in self.roms.values() if collection_id in (r.get("collection_ids") or [])]
        return self._paginate(items, limit, offset)

    def list_roms_by_virtual_collection(self, virtual_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self._log(
            "list_roms_by_virtual_collection",
            (virtual_id,),
            {"limit": limit, "offset": offset},
        )
        self._check_fail(self.list_roms_by_virtual_collection_side_effect)
        items = [r for r in self.roms.values() if virtual_id in (r.get("virtual_collection_ids") or [])]
        return self._paginate(items, limit, offset)

    def list_roms_by_smart_collection(self, smart_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self._log(
            "list_roms_by_smart_collection",
            (smart_id,),
            {"limit": limit, "offset": offset},
        )
        self._check_fail(self.list_roms_by_smart_collection_side_effect)
        items = [r for r in self.roms.values() if smart_id in (r.get("smart_collection_ids") or [])]
        return self._paginate(items, limit, offset)

    def list_collections(self) -> list[dict[str, Any]]:
        self._log("list_collections")
        self._check_fail(self.list_collections_side_effect)
        return [dict(c) for c in self.collections]

    def list_virtual_collections(self, collection_type: str) -> list[dict[str, Any]]:
        self._log("list_virtual_collections", (collection_type,))
        self._check_fail(self.list_virtual_collections_side_effect)
        return [dict(c) for c in self.virtual_collections.get(collection_type, [])]

    def list_smart_collections(self) -> list[dict[str, Any]]:
        self._log("list_smart_collections")
        self._check_fail(self.list_smart_collections_side_effect)
        return [dict(c) for c in self.smart_collections]

    def download_rom_content(
        self,
        rom_id: int,
        filename: str,
        dest: str,
        progress_callback: Any = None,
        *,
        resume: bool = False,
        on_meta: Any = None,
    ) -> None:
        self._log(
            "download_rom_content",
            (rom_id, filename, dest),
            {"progress_callback": progress_callback, "resume": resume, "on_meta": on_meta},
        )
        self._check_fail(self.download_rom_content_side_effect)
        if on_meta is not None:
            on_meta(self.download_range_supported)
        key = f"rom:{rom_id}:{filename}"
        payload = self.download_payloads.get(key, b"")
        self._materialize_download(dest, payload)
        if progress_callback is not None:
            total = len(payload)
            progress_callback(total, total)

    def download_cover(self, cover_url: str, dest: str) -> None:
        self._log("download_cover", (cover_url, dest))
        self._check_fail(self.download_cover_side_effect)
        key = f"cover:{cover_url}"
        payload = self.download_payloads.get(key, b"")
        self._materialize_download(dest, payload)

    # ------------------------------------------------------------------
    # RommFirmwareApi
    # ------------------------------------------------------------------

    def list_firmware(self) -> list[dict[str, Any]]:
        self._log("list_firmware")
        self._check_fail(self.list_firmware_side_effect)
        return [dict(f) for f in self.firmware_files]

    def get_firmware(self, firmware_id: int) -> dict[str, Any]:
        self._log("get_firmware", (firmware_id,))
        self._check_fail(self.get_firmware_side_effect)
        for fw in self.firmware_files:
            if fw.get("id") == firmware_id:
                return dict(fw)
        return {"id": firmware_id}

    def download_firmware(self, firmware_id: int, filename: str, dest: str) -> None:
        self._log("download_firmware", (firmware_id, filename, dest))
        self._check_fail(self.download_firmware_side_effect)
        key = f"firmware:{firmware_id}:{filename}"
        payload = self.download_payloads.get(key, b"")
        self._materialize_download(dest, payload)

    # ------------------------------------------------------------------
    # RommPlaytimeApi (native play-session ingest, ADR-0018)
    # ------------------------------------------------------------------

    def ingest_play_sessions(self, device_id: str, sessions: list[PlaySessionIngestEntry]) -> PlaySessionIngestResponse:
        self._log("ingest_play_sessions", (device_id, sessions))
        self._check_fail(self.ingest_play_sessions_side_effect)
        if self.reject_batch_below_duration_ms > 0:
            bad = [i for i, s in enumerate(sessions) if s["duration_ms"] < self.reject_batch_below_duration_ms]
            if bad:
                # Atomic whole-request rejection: nothing is stored. The 422 body
                # names every offending index, UNLESS the batch is multi-entry and
                # ``mangle_batch_422_detail`` is set — modelling a proxy that
                # strips the detail from a multi-entry validation error.
                detail = (
                    None
                    if (self.mangle_batch_422_detail and len(sessions) > 1)
                    else [{"loc": ["body", "sessions", i], "msg": "end_time must be after start_time"} for i in bad]
                )
                raise RommUnprocessableEntityError("HTTP 422: Unprocessable Entity", detail=detail)
        results: list[PlaySessionIngestResult] = []
        created = 0
        skipped = 0
        for index, session in enumerate(sessions):
            rom_id = session["rom_id"]
            start_time = session["start_time"]
            key = (device_id, rom_id, start_time)
            if key in self._play_session_ledger:
                # Idempotent re-POST: already stored, a successful no-op.
                results.append({"index": index, "status": "duplicate", "id": None})
                skipped += 1
                continue
            if session["duration_ms"] < self.reject_below_duration_ms:
                # Acknowledged but refused (validation) — not stored, terminal.
                results.append({"index": index, "status": "skipped", "detail": "session too short"})
                skipped += 1
                continue
            self._play_session_ledger.add(key)
            session_id = self._next_play_session_id
            self._next_play_session_id += 1
            self.play_sessions.setdefault(rom_id, []).append(
                {
                    "id": session_id,
                    "rom_id": rom_id,
                    "device_id": device_id,
                    "start_time": start_time,
                    "end_time": session["end_time"],
                    "duration_ms": session["duration_ms"],
                }
            )
            results.append({"index": index, "status": "created", "id": session_id})
            created += 1
        return {"results": results, "created_count": created, "skipped_count": skipped}

    def list_play_sessions(self, rom_id: int, limit: int = 100) -> list[dict[str, Any]]:
        self._log("list_play_sessions", (rom_id,), {"limit": limit})
        self._check_fail(self.list_play_sessions_side_effect)
        return [dict(s) for s in self.play_sessions.get(rom_id, [])[:limit]]

    # ------------------------------------------------------------------
    # RommDeviceApi
    # ------------------------------------------------------------------

    def register_device(
        self,
        name: str,
        platform: str,
        client: str,
        client_version: str,
        hostname: str | None = None,
    ) -> dict[str, Any]:
        self._log("register_device", (name, platform, client, client_version), {"hostname": hostname})
        self._check_fail(self.register_device_side_effect)
        device_id = f"device-{self._next_device_id}"
        self._next_device_id += 1
        device = {
            "id": device_id,
            "name": name,
            "platform": platform,
            "client": client,
            "client_version": client_version,
            "hostname": hostname,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.devices.append(device)
        return dict(device)

    def list_devices(self) -> list[dict[str, Any]]:
        self._log("list_devices")
        self._check_fail(self.list_devices_side_effect)
        return [dict(d) for d in self.devices]

    def update_device(self, device_id: str, **fields) -> dict[str, Any]:
        self._log("update_device", (device_id,), fields)
        self._check_fail(self.update_device_side_effect)
        for device in self.devices:
            if str(device.get("id")) == str(device_id):
                device.update({k: v for k, v in fields.items() if v is not None})
                return dict(device)
        return {"id": device_id, **{k: v for k, v in fields.items() if v is not None}}

    # ------------------------------------------------------------------
    # RommSaveApi
    # ------------------------------------------------------------------

    def list_saves(
        self,
        rom_id: int,
        *,
        device_id: str | None = None,
        slot: str | None = None,
    ) -> list[dict[str, Any]]:
        self._log("list_saves", (rom_id,), {"device_id": device_id, "slot": slot})
        self._check_fail(self.list_saves_side_effect)
        saves = [dict(s) for s in self.saves.values() if s.get("rom_id") == rom_id]
        if slot is not None:
            saves = [s for s in saves if s.get("slot") == slot]
        if device_id:
            # ``device_id`` enriches with device_syncs (like the real server).
            # An explicit ``device_syncs`` seeded on the save wins; else compute
            # it per-device from the ledger (never a blanket True).
            for s in saves:
                if "device_syncs" not in s:
                    s["device_syncs"] = self._device_syncs_for(s)
        return saves

    def upload_save(
        self,
        rom_id: int,
        file_path: str,
        emulator: str,
        save_id: int | None = None,
        *,
        device_id: str | None = None,
        slot: str | None = None,
        overwrite: bool = False,
        autocleanup_limit: int | None = None,
    ) -> dict[str, Any]:
        self._log(
            "upload_save",
            (rom_id, file_path, emulator),
            {
                "save_id": save_id,
                "device_id": device_id,
                "slot": slot,
                "overwrite": overwrite,
                "autocleanup_limit": autocleanup_limit,
            },
        )
        self._check_fail(self.upload_save_side_effect)
        stamp = datetime.now(UTC)
        now = stamp.isoformat()
        # Path algebra only — basename without importing os.path globally.
        last_sep = max(file_path.rfind("/"), file_path.rfind("\\"))
        filename = file_path[last_sep + 1 :] if last_sep >= 0 else file_path

        # PUT path: update the tracked save in place (no new version, no gate).
        if save_id is not None and save_id in self.saves:
            entry = self.saves[save_id]
            entry["updated_at"] = now
            entry["emulator"] = emulator
            return dict(entry)

        # POST path. On a slot POST the real server refuses (409) to stack onto
        # a slot this device hasn't synced, and tags the stored filename so
        # versions never collide. The gate runs BEFORE any mutation and lets
        # ``RommConflictError`` propagate uncaught, exactly like the adapter.
        stored_filename = filename
        if slot is not None:
            slot_saves = [s for s in self.saves.values() if s.get("rom_id") == rom_id and s.get("slot") == slot]
            check_add_save_conflict(
                device_id=device_id,
                slot=slot,
                overwrite=overwrite,
                slot_saves=slot_saves,
                sync_ledger=self._device_sync_ledger,
            )
            stored_filename = tag_filename(filename, stamp.strftime("%Y-%m-%d_%H-%M-%S"))

        new_save_id = self._next_save_id
        self._next_save_id += 1
        entry = {
            "id": new_save_id,
            "rom_id": rom_id,
            "file_name": stored_filename,
            "updated_at": now,
            "emulator": emulator,
            "slot": slot,
            "download_path": f"/saves/{stored_filename}",
        }
        self.saves[new_save_id] = entry
        return dict(entry)

    def download_save(self, save_id: int, dest_path: str) -> None:
        self._log("download_save", (save_id, dest_path))
        self._check_fail(self.download_save_side_effect)
        payload = self._save_content.get(save_id) or self.download_payloads.get(f"save:{save_id}", b"")
        self._materialize_download(dest_path, payload)

    def download_save_content(
        self,
        save_id: int,
        dest_path: str,
        *,
        device_id: str | None = None,
        optimistic: bool = True,
    ) -> None:
        self._log(
            "download_save_content",
            (save_id, dest_path),
            {"device_id": device_id, "optimistic": optimistic},
        )
        self._check_fail(self.download_save_content_side_effect)
        payload = self._save_content.get(save_id) or self.download_payloads.get(f"save:{save_id}", b"")
        self._materialize_download(dest_path, payload)
        if optimistic:
            # Optimistic download pre-acks the DeviceSaveSync row server-side,
            # so this device becomes is_current on the save.
            self._record_device_sync(save_id, device_id)

    def confirm_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        self._log("confirm_download", (save_id, device_id))
        self._check_fail(self.confirm_download_side_effect)
        self._record_device_sync(save_id, device_id)
        return {"status": "ok"}

    def get_save_summary(self, rom_id: int, device_id: str | None = None) -> dict[str, Any]:
        self._log("get_save_summary", (rom_id,), {"device_id": device_id})
        self._check_fail(self.get_save_summary_side_effect)
        slots: dict[str | None, list[dict[str, Any]]] = {}
        for s in self.saves.values():
            if s.get("rom_id") == rom_id:
                slots.setdefault(s.get("slot"), []).append(s)
        return {
            "total_count": sum(len(saves) for saves in slots.values()),
            "slots": [
                {
                    "slot": slot_name,
                    "count": len(saves),
                    "latest": max(saves, key=lambda s: s.get("updated_at", "")),
                }
                for slot_name, saves in slots.items()
            ],
        }

    def delete_server_saves(self, save_ids: list[int]) -> dict[str, Any]:
        self._log("delete_server_saves", (save_ids,))
        self._check_fail(self.delete_server_saves_side_effect)
        for sid in save_ids:
            self.saves.pop(sid, None)
            self._save_content.pop(sid, None)
        return {"deleted": len(save_ids)}

    def negotiate_sync(self, device_id: str, saves: list[ClientSaveState]) -> SyncNegotiateResponse:
        self._log("negotiate_sync", (device_id, saves))
        self._check_fail()
        return {
            "session_id": 1,
            "operations": [],
            "total_upload": 0,
            "total_download": 0,
            "total_conflict": 0,
            "total_no_op": 0,
        }

    def complete_sync_session(
        self,
        session_id: int,
        *,
        operations_completed: int = 0,
        operations_failed: int = 0,
    ) -> SyncCompleteResponse:
        self._log(
            "complete_sync_session",
            (session_id,),
            {
                "operations_completed": operations_completed,
                "operations_failed": operations_failed,
            },
        )
        self._check_fail()
        return {
            "session": {
                "id": session_id,
                "device_id": "",
                "user_id": 0,
                "status": "completed",
                "initiated_at": "",
                "operations_planned": 0,
                "operations_completed": operations_completed,
                "operations_failed": operations_failed,
                "created_at": "",
                "updated_at": "",
            }
        }

    # ------------------------------------------------------------------
    # RommTokenApi
    # ------------------------------------------------------------------

    def mint_client_token(self, username: str, password: str, *, token_name: str) -> dict[str, Any]:
        self._log("mint_client_token", (username, password), {"token_name": token_name})
        self._check_fail(self.mint_client_token_side_effect)
        return dict(self.mint_client_token_response)

    def delete_client_token(self, username: str, password: str, *, token_id: int) -> None:
        self._log("delete_client_token", (username, password), {"token_id": token_id})
        self._check_fail(self.delete_client_token_side_effect)
        self.deleted_token_ids.append(token_id)

    def exchange_pairing_code(self, code: str) -> dict[str, Any]:
        self._log("exchange_pairing_code", (code,))
        self._check_fail(self.exchange_pairing_code_side_effect)
        return dict(self.exchange_pairing_code_response)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def set_server_save_content(self, save_id: int, content: bytes) -> None:
        """Stage server-side bytes returned by the next ``download_save*`` call."""
        self._save_content[save_id] = content

    def stage_device_sync(self, save_id: int, device_id: str, last_synced_at: str) -> None:
        """Record that *device_id* last synced *save_id* at *last_synced_at*.

        Drives ``list_saves``' per-device ``device_syncs`` / ``is_current`` and
        the add_save 409 gate directly, mirroring a server DeviceSaveSync row.
        Model a *stale* device with a ``last_synced_at`` older than the save's
        ``updated_at``; model a *never-synced* device by omitting the call.
        """
        self._device_sync_ledger[(device_id, save_id)] = last_synced_at

    def seed_foreign_save(
        self,
        rom_id: int,
        *,
        save_id: int | None = None,
        uploaded_by: str = "device-B",
        slot: str | None = "default",
        filename: str = "pokemon.srm",
        updated_at: str = "2026-02-17T06:00:00Z",
        content: bytes | None = b"foreign-save",
        file_size_bytes: int | None = None,
        last_synced_at: str | None = None,
    ) -> dict[str, Any]:
        """Seed a slot save owned by another device and mark that device current.

        Models the foreign-origin case: a save the local (querying) device has
        never synced. ``uploaded_by`` is recorded as current on it via the sync
        ledger; the local device gets no ledger entry, so its ``list_saves``
        shows no ``is_current`` for it and an ``overwrite=false`` POST into the
        slot 409s. Returns a copy of the seeded save dict.
        """
        if save_id is None:
            save_id = self._next_save_id
            self._next_save_id += 1
        if file_size_bytes is None:
            file_size_bytes = len(content) if content is not None else 0
        entry: dict[str, Any] = {
            "id": save_id,
            "rom_id": rom_id,
            "file_name": filename,
            "slot": slot,
            "updated_at": updated_at,
            "file_size_bytes": file_size_bytes,
            "emulator": "",
            "download_path": f"/saves/{filename}",
        }
        self.saves[save_id] = entry
        if content is not None:
            self._save_content[save_id] = content
        self.stage_device_sync(save_id, uploaded_by, last_synced_at or updated_at)
        return dict(entry)

    def _record_device_sync(self, save_id: int, device_id: str | None) -> None:
        """Record *device_id* as having synced *save_id* at the save's updated_at.

        Mirrors the server writing a DeviceSaveSync row on confirm / optimistic
        download so a later ``list_saves`` reports ``is_current`` for this
        device. Falls back to the current time when the save is not tracked.
        """
        if not device_id:
            return
        entry = self.saves.get(save_id)
        updated_at = entry.get("updated_at") if entry else None
        self._device_sync_ledger[(device_id, save_id)] = updated_at or datetime.now(UTC).isoformat()

    def _device_syncs_for(self, save: dict[str, Any]) -> list[dict[str, Any]]:
        """Compute a save's ``device_syncs`` list from the sync ledger.

        One entry per device that has ever synced this save, each with its own
        ``is_current`` relative to the save's current ``updated_at`` — multi-
        device / foreign-origin aware, not a blanket True for the querier.
        """
        save_id = save.get("id")
        updated_at = save.get("updated_at", "")
        return [
            {
                "device_id": d_id,
                "is_current": compute_is_current(last_synced_at, updated_at),
                "last_synced_at": last_synced_at,
            }
            for (d_id, s_id), last_synced_at in self._device_sync_ledger.items()
            if s_id == save_id
        ]
