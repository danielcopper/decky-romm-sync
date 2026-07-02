"""In-memory ``RommSaveApi`` (save/note methods) implementation for service tests."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fakes._romm_save_semantics import check_add_save_conflict, compute_is_current, tag_filename

if TYPE_CHECKING:
    from models.sync import (
        ClientSaveState,
        SyncCompleteResponse,
        SyncNegotiateResponse,
        SyncOperation,
        SyncPlaySessionEntry,
    )

    from services.protocols import SaveFileStore


class FakeSaveApi:
    """In-memory fake that satisfies ``RommSaveApi`` save/note methods without HTTP.

    Only save, note, and download_save methods are implemented.
    ROM, firmware, and platform methods raise NotImplementedError — use MagicMock()
    when those methods are needed.

    Server-side save bytes live in ``_save_content`` (``save_id -> bytes``).
    All filesystem I/O is delegated to the injected ``save_file_store`` adapter
    so this fake never imports ``os``, ``open``, or ``shutil`` directly. When
    ``save_file_store`` is None the fake is fully in-memory: uploads record a
    zero-byte snapshot and downloads write the default zero-byte payload
    nowhere.
    """

    def __init__(self, save_file_store: SaveFileStore | None = None) -> None:
        self.save_file_store: SaveFileStore | None = save_file_store
        self.saves: dict[int, dict[str, Any]] = {}  # save_id -> save dict
        self.roms: dict[int, dict[str, Any]] = {}  # rom_id -> rom detail dict
        self.notes: dict[int, list[dict[str, Any]]] = {}  # rom_id -> [note dicts]
        self.uploaded_files: dict[int, str] = {}  # save_id -> source file_path (log only)
        self.downloaded_files: dict[int, str] = {}  # save_id -> dest_path (log only)
        self._save_content: dict[int, bytes] = {}  # save_id -> server-side bytes
        # DeviceSaveSync rows: (device_id, save_id) -> last_synced_at (ISO). The
        # server writes one on confirm/optimistic-download; list_saves computes
        # each save's ``device_syncs`` (and is_current) from it, and the
        # add_save 409 gate reads it. Seed directly via ``stage_device_sync``.
        self._device_sync_ledger: dict[tuple[str, int], str] = {}
        self.call_log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._next_save_id = 1000
        self._next_note_id = 2000
        self._fail_on_next: Exception | None = None
        self._fail_download_on: dict[int, Exception] = {}  # save_id -> exc for download_save_content
        self.heartbeat_raises: Exception | None = None
        self._registered_devices: list[dict[str, Any]] = []
        self._next_device_id = 1
        # Negotiate (4.9 Device Sync): the ops the next negotiate_sync returns and
        # the session id it opens. Default is an empty plan (no ops) — tests that
        # drive the negotiate path stage ops via ``stage_negotiate``.
        self._negotiate_operations: list[SyncOperation] = []
        self._negotiate_session_id = 1
        # When set, complete_sync_session raises this AFTER logging the call, to
        # exercise the non-fatal session-close path without failing the run.
        self.complete_raises: Exception | None = None

    def fail_on_next(self, exc: Exception) -> None:
        """Make the next call raise the given exception."""
        self._fail_on_next = exc

    def fail_download_on(self, save_id: int, exc: Exception) -> None:
        """Make ``download_save_content`` raise *exc* for a specific *save_id*.

        Unlike :meth:`fail_on_next` (which fires on the next call of any method),
        this targets one save's download so a multi-target switch can fail just
        one leg and exercise the partial-failure path. Fires every time the save
        is requested until cleared.
        """
        self._fail_download_on[save_id] = exc

    def set_server_save_content(self, save_id: int, content: bytes) -> None:
        """Stage server-side bytes for *save_id* without writing to disk.

        Tests use this to seed the bytes a later ``download_save_content``
        will write to ``dest_path``. Mirrors the in-memory ``_save_content``
        dict directly so callers don't have to reach into a private name.
        """
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

    def _check_fail(self) -> None:
        if self._fail_on_next is not None:
            exc = self._fail_on_next
            self._fail_on_next = None
            raise exc

    def _basename(self, path: str) -> str:
        # Path algebra only — split on both separators so callers using
        # tmp_path style absolute paths still get the file component.
        last_sep = max(path.rfind("/"), path.rfind("\\"))
        return path[last_sep + 1 :] if last_sep >= 0 else path

    def _capture_upload(self, save_id: int, file_path: str) -> int:
        """Read bytes from *file_path* via the injected adapter and return size.

        When no ``save_file_store`` adapter is wired, records empty bytes (size 0)
        — tests that exercise size/hash semantics must wire an adapter.
        """
        if self.save_file_store is None:
            self._save_content[save_id] = b""
            return 0
        if not self.save_file_store.is_file(file_path):
            self._save_content[save_id] = b""
            return 0
        data = self.save_file_store.read_bytes(file_path)
        self._save_content[save_id] = data
        return len(data)

    def _materialize_download(self, save_id: int, dest_path: str) -> None:
        """Write the staged bytes for *save_id* to *dest_path*.

        Resolution order:
        1. ``_save_content[save_id]`` — bytes captured at upload or staged
           via ``set_server_save_content``.
        2. ``uploaded_files[save_id]`` — legacy staging where a test wrote
           a file to disk and pointed at it; we re-read via the adapter.
        3. Fallback to 1024 zero-bytes so callers always get a file.
        """
        if self.save_file_store is None:
            return
        if save_id in self._save_content:
            data = self._save_content[save_id]
        elif save_id in self.uploaded_files and self.save_file_store.is_file(self.uploaded_files[save_id]):
            data = self.save_file_store.read_bytes(self.uploaded_files[save_id])
        else:
            data = b"\x00" * 1024
        pathlib.Path(dest_path).write_bytes(data)

    # ------------------------------------------------------------------
    # Unimplemented RomM API methods (use MagicMock for these)
    # ------------------------------------------------------------------

    def set_version(self, version: str | None) -> None:
        self.version = version

    def get_version(self) -> str | None:
        return getattr(self, "version", None)

    def heartbeat(self) -> dict[str, Any]:
        self.call_log.append(("heartbeat", (), {}))
        if self.heartbeat_raises is not None:
            raise self.heartbeat_raises
        return {"status": "ok"}

    def heartbeat_once(self) -> dict[str, Any]:
        self.call_log.append(("heartbeat_once", (), {}))
        if self.heartbeat_raises is not None:
            raise self.heartbeat_raises
        return {"status": "ok"}

    def list_platforms(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_current_user(self) -> dict[str, Any]:
        raise NotImplementedError

    def get_rom(self, rom_id: int) -> dict[str, Any]:
        raise NotImplementedError

    def list_roms(self, platform_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        raise NotImplementedError

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict[str, Any]:
        raise NotImplementedError

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
        raise NotImplementedError

    def download_cover(self, cover_url: str, dest: str) -> None:
        raise NotImplementedError

    def list_firmware(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_firmware(self, firmware_id: int) -> dict[str, Any]:
        raise NotImplementedError

    def download_firmware(self, firmware_id: int, filename: str, dest: str) -> None:
        raise NotImplementedError

    def list_collections(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_virtual_collections(self, collection_type: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_roms_by_collection(self, collection_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        raise NotImplementedError

    def list_roms_by_virtual_collection(self, virtual_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        raise NotImplementedError

    def delete_server_saves(self, save_ids: list[int]) -> dict[str, Any]:
        self.call_log.append(("delete_server_saves", (save_ids,), {}))
        self._check_fail()
        for sid in save_ids:
            self.saves.pop(sid, None)
            self._save_content.pop(sid, None)
        return {"deleted": len(save_ids)}

    def stage_negotiate(self, operations: list[SyncOperation], *, session_id: int = 1) -> None:
        """Script the operations (and session id) the next ``negotiate_sync`` returns.

        Each op is a plain ``SyncOperation`` dict
        (``{"action", "rom_id", "file_name", "slot", ...}``); the negotiate path
        dispatches them through the real executors, so an ``upload`` op POSTs/PUTs
        a local save, a ``download`` op pulls a server save, etc.
        """
        self._negotiate_operations = list(operations)
        self._negotiate_session_id = session_id

    def negotiate_sync(self, device_id: str, saves: list[ClientSaveState]) -> SyncNegotiateResponse:
        self.call_log.append(("negotiate_sync", (device_id, saves), {}))
        self._check_fail()
        ops = list(self._negotiate_operations)
        totals = {"upload": 0, "download": 0, "conflict": 0, "no_op": 0}
        for op in ops:
            totals[op["action"]] += 1
        return {
            "session_id": self._negotiate_session_id,
            "operations": ops,
            "total_upload": totals["upload"],
            "total_download": totals["download"],
            "total_conflict": totals["conflict"],
            "total_no_op": totals["no_op"],
        }

    def complete_sync_session(
        self,
        session_id: int,
        *,
        operations_completed: int = 0,
        operations_failed: int = 0,
        play_sessions: list[SyncPlaySessionEntry] | None = None,
    ) -> SyncCompleteResponse:
        self.call_log.append(
            (
                "complete_sync_session",
                (session_id,),
                {
                    "operations_completed": operations_completed,
                    "operations_failed": operations_failed,
                    "play_sessions": play_sessions,
                },
            )
        )
        if self.complete_raises is not None:
            raise self.complete_raises
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

    def register_device(
        self,
        name: str,
        platform: str,
        client: str,
        client_version: str,
        hostname: str | None = None,
    ) -> dict[str, Any]:
        self.call_log.append(("register_device", (name, platform, client, client_version), {"hostname": hostname}))
        self._check_fail()
        device_id = f"device-{self._next_device_id}"
        self._next_device_id += 1
        device = {"id": device_id, "name": name, "created_at": datetime.now(UTC).isoformat()}
        self._registered_devices.append(device)
        return device

    def list_devices(self) -> list[dict[str, Any]]:
        self.call_log.append(("list_devices", (), {}))
        self._check_fail()
        return list(self._registered_devices)

    def update_device(self, device_id: str, **fields) -> dict[str, Any]:
        self.call_log.append(("update_device", (device_id,), fields))
        self._check_fail()
        for device in self._registered_devices:
            if str(device.get("id")) == str(device_id):
                device.update({k: v for k, v in fields.items() if v is not None})
                return dict(device)
        return {"id": device_id, **{k: v for k, v in fields.items() if v is not None}}

    def download_save_content(
        self,
        save_id: int,
        dest_path: str,
        *,
        device_id: str | None = None,
        optimistic: bool = True,
    ) -> None:
        self.call_log.append(
            ("download_save_content", (save_id, dest_path), {"device_id": device_id, "optimistic": optimistic})
        )
        self._check_fail()
        if save_id in self._fail_download_on:
            raise self._fail_download_on[save_id]

        self.downloaded_files[save_id] = dest_path
        self._materialize_download(save_id, dest_path)
        if optimistic:
            # Optimistic download pre-acks the DeviceSaveSync row server-side,
            # so this device becomes is_current on the save.
            self._record_device_sync(save_id, device_id)

    def confirm_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        self.call_log.append(("confirm_download", (save_id, device_id), {}))
        self._check_fail()
        self._record_device_sync(save_id, device_id)
        return {"status": "ok"}

    def get_save_summary(self, rom_id: int, device_id: str | None = None) -> dict[str, Any]:
        self.call_log.append(("get_save_summary", (rom_id,), {"device_id": device_id}))
        self._check_fail()
        slots: dict[str | None, list[dict[str, Any]]] = {}
        for s in self.saves.values():
            if s.get("rom_id") == rom_id:
                slot = s.get("slot")
                slots.setdefault(slot, []).append(s)
        return {
            "total_count": sum(len(saves) for saves in slots.values()),
            "slots": [
                {
                    "slot": slot_name,  # None for legacy saves (no slot) — preserve as-is
                    "count": len(saves),
                    "latest": max(saves, key=lambda s: s.get("updated_at", "")),
                }
                for slot_name, saves in slots.items()
            ],
        }

    # ------------------------------------------------------------------
    # Implemented save/note methods
    # ------------------------------------------------------------------

    def list_saves(
        self,
        rom_id: int,
        *,
        device_id: str | None = None,
        slot: str | None = None,
    ) -> list[dict[str, Any]]:
        self.call_log.append(("list_saves", (rom_id,), {"device_id": device_id, "slot": slot}))
        self._check_fail()
        saves = [s for s in self.saves.values() if s.get("rom_id") == rom_id]
        if slot is not None:
            saves = [s for s in saves if s.get("slot") == slot]
        result: list[dict[str, Any]] = []
        for s in saves:
            entry = dict(s)
            # ``device_id`` enriches with device_syncs (like the real server).
            # An explicit ``device_syncs`` seeded on the stored save wins; else
            # compute it per-device from the ledger (never a blanket True).
            if device_id and "device_syncs" not in entry:
                entry["device_syncs"] = self._device_syncs_for(s)
            result.append(entry)
        return result

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
        self.call_log.append(
            (
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
        )
        self._check_fail()

        filename = self._basename(file_path)
        stamp = datetime.now(UTC)
        now = stamp.isoformat()

        # PUT path: update the tracked save in place (no new version, no gate).
        if save_id and save_id in self.saves:
            size = self._capture_upload(save_id, file_path)
            entry = self.saves[save_id]
            entry["updated_at"] = now
            entry["file_size_bytes"] = size
            entry["emulator"] = emulator
            self.uploaded_files[save_id] = file_path
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
        else:
            # Legacy (no slot) uploads upsert by filename — the real server does
            # not tag these, so a re-upload of the same file updates in place.
            existing = next(
                (s for s in self.saves.values() if s.get("rom_id") == rom_id and s.get("file_name") == filename),
                None,
            )
            if existing is not None:
                save_id = existing["id"]
                assert save_id is not None
                size = self._capture_upload(save_id, file_path)
                existing["updated_at"] = now
                existing["file_size_bytes"] = size
                existing["emulator"] = emulator
                self.uploaded_files[save_id] = file_path
                return dict(existing)

        save_id = self._next_save_id
        self._next_save_id += 1
        size = self._capture_upload(save_id, file_path)
        entry = {
            "id": save_id,
            "rom_id": rom_id,
            "file_name": stored_filename,
            "slot": slot,
            "updated_at": now,
            "file_size_bytes": size,
            "emulator": emulator,
            "download_path": f"/saves/{stored_filename}",
        }
        self.saves[save_id] = entry
        self.uploaded_files[save_id] = file_path
        return dict(entry)

    def download_save(self, save_id: int, dest_path: str) -> None:
        self.call_log.append(("download_save", (save_id, dest_path), {}))
        self._check_fail()

        self.downloaded_files[save_id] = dest_path
        self._materialize_download(save_id, dest_path)

    def get_rom_with_notes(self, rom_id: int) -> dict[str, Any]:
        self.call_log.append(("get_rom_with_notes", (rom_id,), {}))
        self._check_fail()
        detail = self.roms.get(rom_id, {"id": rom_id})
        # Attach notes
        detail = dict(detail)
        detail["all_user_notes"] = self.notes.get(rom_id, [])
        return detail

    def create_note(self, rom_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self.call_log.append(("create_note", (rom_id, data), {}))
        self._check_fail()
        note_id = self._next_note_id
        self._next_note_id += 1
        note = {"id": note_id, "rom_id": rom_id, **data}
        self.notes.setdefault(rom_id, []).append(note)
        return dict(note)

    def update_note(self, rom_id: int, note_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self.call_log.append(("update_note", (rom_id, note_id, data), {}))
        self._check_fail()
        for notes in self.notes.values():
            for note in notes:
                if note.get("id") == note_id:
                    note.update(data)
                    return dict(note)
        return {"id": note_id, "rom_id": rom_id, **data}
