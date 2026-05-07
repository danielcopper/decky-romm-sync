"""Pure helpers for the saves package — no state, no I/O."""

from __future__ import annotations


def _compute_uploaded_by_us(
    server_save: dict | None,
    own_upload_ids: list[int] | None,
) -> bool | None:
    """Three-way uploader attribution flag.

    Returns True/False when own_upload_ids is known for this ROM (we can tell
    whether this installation POSTed the save), or None for legacy ROM state
    without the ``own_upload_ids`` field (attribution unknown).
    """
    if server_save is None or own_upload_ids is None:
        return None
    sid = server_save.get("id")
    if sid is None:
        return None
    return sid in own_upload_ids


def _local_save_target(server_save: dict, rom_name: str) -> str:
    """The canonical local filename for a server save: ``<rom_name>.<ext>``.

    ``rom_name`` is the deterministic identity from RetroArch's
    perspective — it's the ROM file's basename without extension, the
    same string RetroArch uses to look up SRAM. Callers must have
    already resolved the ROM via ``_get_rom_save_info`` (which only
    returns when the ROM is actually installed); there is no fallback
    to server-derived names because those can mismatch RetroArch's
    actual lookup path and silently break the sync.
    """
    ext = server_save.get("file_extension", "srm")
    return f"{rom_name}.{ext}"
