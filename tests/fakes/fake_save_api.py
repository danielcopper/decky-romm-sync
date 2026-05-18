"""In-memory ``RommSaveApi`` (save/note methods) implementation for service tests."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
        self.saves: dict[int, dict] = {}  # save_id -> save dict
        self.roms: dict[int, dict] = {}  # rom_id -> rom detail dict
        self.notes: dict[int, list[dict]] = {}  # rom_id -> [note dicts]
        self.uploaded_files: dict[int, str] = {}  # save_id -> source file_path (log only)
        self.downloaded_files: dict[int, str] = {}  # save_id -> dest_path (log only)
        self._save_content: dict[int, bytes] = {}  # save_id -> server-side bytes
        self.call_log: list[tuple[str, tuple, dict]] = []
        self._next_save_id = 1000
        self._next_note_id = 2000
        self._fail_on_next: Exception | None = None
        self.heartbeat_raises: Exception | None = None
        self._registered_devices: list[dict] = []
        self._next_device_id = 1

    def fail_on_next(self, exc: Exception) -> None:
        """Make the next call raise the given exception."""
        self._fail_on_next = exc

    def set_server_save_content(self, save_id: int, content: bytes) -> None:
        """Stage server-side bytes for *save_id* without writing to disk.

        Tests use this to seed the bytes a later ``download_save_content``
        will write to ``dest_path``. Mirrors the in-memory ``_save_content``
        dict directly so callers don't have to reach into a private name.
        """
        self._save_content[save_id] = content

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

    def heartbeat(self) -> dict:
        if self.heartbeat_raises is not None:
            raise self.heartbeat_raises
        return {"status": "ok"}

    def list_platforms(self) -> list[dict]:
        raise NotImplementedError

    def get_current_user(self) -> dict:
        raise NotImplementedError

    def get_rom(self, rom_id: int) -> dict:
        raise NotImplementedError

    def list_roms(self, platform_id: int, limit: int = 50, offset: int = 0) -> dict:
        raise NotImplementedError

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict:
        raise NotImplementedError

    def download_rom_content(
        self,
        rom_id: int,
        filename: str,
        dest: str,
        progress_callback: Any = None,
    ) -> None:
        raise NotImplementedError

    def download_cover(self, cover_url: str, dest: str) -> None:
        raise NotImplementedError

    def list_firmware(self) -> list[dict]:
        raise NotImplementedError

    def get_firmware(self, firmware_id: int) -> dict:
        raise NotImplementedError

    def download_firmware(self, firmware_id: int, filename: str, dest: str) -> None:
        raise NotImplementedError

    def list_collections(self) -> list[dict]:
        raise NotImplementedError

    def list_virtual_collections(self, collection_type: str) -> list[dict]:
        raise NotImplementedError

    def list_roms_by_collection(self, collection_id: int, limit: int = 50, offset: int = 0) -> dict:
        raise NotImplementedError

    def list_roms_by_virtual_collection(self, virtual_id: str, limit: int = 50, offset: int = 0) -> dict:
        raise NotImplementedError

    def delete_server_saves(self, save_ids: list[int]) -> dict:
        self.call_log.append(("delete_server_saves", (save_ids,), {}))
        self._check_fail()
        for sid in save_ids:
            self.saves.pop(sid, None)
            self._save_content.pop(sid, None)
        return {"deleted": len(save_ids)}

    def register_device(self, name: str, platform: str, client: str, client_version: str) -> dict:
        self.call_log.append(("register_device", (name, platform, client, client_version), {}))
        self._check_fail()
        device_id = f"device-{self._next_device_id}"
        self._next_device_id += 1
        device = {"id": device_id, "name": name, "created_at": datetime.now(UTC).isoformat()}
        self._registered_devices.append(device)
        return device

    def list_devices(self) -> list[dict]:
        self.call_log.append(("list_devices", (), {}))
        self._check_fail()
        return list(self._registered_devices)

    def update_device(self, device_id: str, **fields) -> dict:
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

        self.downloaded_files[save_id] = dest_path
        self._materialize_download(save_id, dest_path)

    def confirm_download(self, save_id: int, device_id: str) -> dict:
        self.call_log.append(("confirm_download", (save_id, device_id), {}))
        self._check_fail()
        return {"status": "ok"}

    def get_save_summary(self, rom_id: int, device_id: str | None = None) -> dict:
        self.call_log.append(("get_save_summary", (rom_id,), {"device_id": device_id}))
        self._check_fail()
        slots: dict[str | None, list[dict]] = {}
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
    ) -> list[dict]:
        self.call_log.append(("list_saves", (rom_id,), {"device_id": device_id, "slot": slot}))
        self._check_fail()
        saves = [s for s in self.saves.values() if s.get("rom_id") == rom_id]
        if slot is not None:
            saves = [s for s in saves if s.get("slot") == slot]
        if device_id:
            # Simulate server adding device_syncs when device_id is provided
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
    ) -> dict:
        self.call_log.append(
            (
                "upload_save",
                (rom_id, file_path, emulator),
                {
                    "save_id": save_id,
                    "device_id": device_id,
                    "slot": slot,
                    "overwrite": overwrite,
                },
            )
        )
        self._check_fail()

        filename = self._basename(file_path)
        now = datetime.now(UTC).isoformat()

        if save_id and save_id in self.saves:
            size = self._capture_upload(save_id, file_path)
            entry = self.saves[save_id]
            entry["updated_at"] = now
            entry["file_size_bytes"] = size
            entry["emulator"] = emulator
        else:
            # Check for upsert by filename
            existing = None
            for s in self.saves.values():
                if s.get("rom_id") == rom_id and s.get("file_name") == filename:
                    existing = s
                    break
            if existing:
                save_id = existing["id"]
                assert save_id is not None
                size = self._capture_upload(save_id, file_path)
                existing["updated_at"] = now
                existing["file_size_bytes"] = size
                existing["emulator"] = emulator
                entry = existing
            else:
                save_id = self._next_save_id
                self._next_save_id += 1
                size = self._capture_upload(save_id, file_path)
                entry = {
                    "id": save_id,
                    "rom_id": rom_id,
                    "file_name": filename,
                    "updated_at": now,
                    "file_size_bytes": size,
                    "emulator": emulator,
                    "download_path": f"/saves/{filename}",
                }
                self.saves[save_id] = entry

        assert save_id is not None
        self.uploaded_files[save_id] = file_path
        return dict(entry)

    def download_save(self, save_id: int, dest_path: str) -> None:
        self.call_log.append(("download_save", (save_id, dest_path), {}))
        self._check_fail()

        self.downloaded_files[save_id] = dest_path
        self._materialize_download(save_id, dest_path)

    def get_rom_with_notes(self, rom_id: int) -> dict:
        self.call_log.append(("get_rom_with_notes", (rom_id,), {}))
        self._check_fail()
        detail = self.roms.get(rom_id, {"id": rom_id})
        # Attach notes
        detail = dict(detail)
        detail["all_user_notes"] = self.notes.get(rom_id, [])
        return detail

    def create_note(self, rom_id: int, data: dict) -> dict:
        self.call_log.append(("create_note", (rom_id, data), {}))
        self._check_fail()
        note_id = self._next_note_id
        self._next_note_id += 1
        note = {"id": note_id, "rom_id": rom_id, **data}
        self.notes.setdefault(rom_id, []).append(note)
        return dict(note)

    def update_note(self, rom_id: int, note_id: int, data: dict) -> dict:
        self.call_log.append(("update_note", (rom_id, note_id, data), {}))
        self._check_fail()
        for notes in self.notes.values():
            for note in notes:
                if note.get("id") == note_id:
                    note.update(data)
                    return dict(note)
        return {"id": note_id, "rom_id": rom_id, **data}
