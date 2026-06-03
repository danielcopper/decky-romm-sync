"""In-memory ``RommApi`` implementation covering every RomM Protocol surface.

Use this fake anywhere a service needs a RomM transport in tests. It
implements every method declared on the per-domain Protocols in
``services.protocols.transport`` (``RommLibraryApi``, ``RommSyncApi``,
``RommConnectionApi``, ``RommAchievementsApi``, ``RommFirmwareApi``,
``RommPlaytimeApi``, ``RommDeviceApi``, ``RommPlatformReader``,
``RommRomReader``, ``RommSaveApi``, ``RommVersion``) so a single instance
satisfies any of them via duck typing.

Seed in-memory state directly on the public attributes
(``platforms`` / ``roms`` / ``firmware_files`` / ``collections`` /
``virtual_collections`` / ``smart_collections`` / ``notes`` / ``saves`` /
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
from typing import Any


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
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.saves: dict[int, dict[str, Any]] = {}
        self.devices: list[dict[str, Any]] = []
        self.current_user: dict[str, Any] = {"id": 1, "username": "tester"}
        self.heartbeat_response: dict[str, Any] = {"status": "ok"}
        self._version: str | None = None
        self._save_content: dict[int, bytes] = {}

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
        self.download_cover_side_effect: Exception | None = None
        self.get_current_user_side_effect: Exception | None = None
        self.get_rom_with_notes_side_effect: Exception | None = None
        self.create_note_side_effect: Exception | None = None
        self.update_note_side_effect: Exception | None = None
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

        # Observability — every method records ``(name, args, kwargs)``.
        self.call_log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        # Internal id counters for synthesised entities.
        self._next_save_id = 1000
        self._next_note_id = 2000
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
    ) -> None:
        self._log(
            "download_rom_content",
            (rom_id, filename, dest),
            {"progress_callback": progress_callback},
        )
        self._check_fail(self.download_rom_content_side_effect)
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
    # RommPlaytimeApi
    # ------------------------------------------------------------------

    def get_rom_with_notes(self, rom_id: int) -> dict[str, Any]:
        self._log("get_rom_with_notes", (rom_id,))
        self._check_fail(self.get_rom_with_notes_side_effect)
        detail = dict(self.roms.get(rom_id, {"id": rom_id}))
        detail["all_user_notes"] = [dict(n) for n in self.notes.get(rom_id, [])]
        return detail

    def create_note(self, rom_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self._log("create_note", (rom_id, data))
        self._check_fail(self.create_note_side_effect)
        note_id = self._next_note_id
        self._next_note_id += 1
        note = {"id": note_id, "rom_id": rom_id, **data}
        self.notes.setdefault(rom_id, []).append(note)
        return dict(note)

    def update_note(self, rom_id: int, note_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self._log("update_note", (rom_id, note_id, data))
        self._check_fail(self.update_note_side_effect)
        for notes in self.notes.values():
            for note in notes:
                if note.get("id") == note_id:
                    note.update(data)
                    return dict(note)
        return {"id": note_id, "rom_id": rom_id, **data}

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
            for s in saves:
                if "device_syncs" not in s:
                    s["device_syncs"] = [{"device_id": device_id, "is_current": True}]
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
    ) -> dict[str, Any]:
        self._log(
            "upload_save",
            (rom_id, file_path, emulator),
            {
                "save_id": save_id,
                "device_id": device_id,
                "slot": slot,
                "overwrite": overwrite,
            },
        )
        self._check_fail(self.upload_save_side_effect)
        now = datetime.now(UTC).isoformat()
        # Path algebra only — basename without importing os.path globally.
        last_sep = max(file_path.rfind("/"), file_path.rfind("\\"))
        filename = file_path[last_sep + 1 :] if last_sep >= 0 else file_path

        if save_id is not None and save_id in self.saves:
            entry = self.saves[save_id]
            entry["updated_at"] = now
            entry["emulator"] = emulator
            return dict(entry)

        new_save_id = self._next_save_id
        self._next_save_id += 1
        entry = {
            "id": new_save_id,
            "rom_id": rom_id,
            "file_name": filename,
            "updated_at": now,
            "emulator": emulator,
            "slot": slot,
            "download_path": f"/saves/{filename}",
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

    def confirm_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        self._log("confirm_download", (save_id, device_id))
        self._check_fail(self.confirm_download_side_effect)
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

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def set_server_save_content(self, save_id: int, content: bytes) -> None:
        """Stage server-side bytes returned by the next ``download_save*`` call."""
        self._save_content[save_id] = content
