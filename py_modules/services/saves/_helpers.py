"""Cross-cutting helpers for the saves package — utilities shared across sub-services."""

from __future__ import annotations

import logging

from domain.save_path import sanitize_save_filename

_logger = logging.getLogger(__name__)


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
    target = f"{rom_name}.{ext}"
    try:
        sanitized = sanitize_save_filename(target)
    except ValueError:
        # Server returned an extension that produces an unusable filename
        # (NUL byte, ``"."``, ``".."``, …). Fall back to the safe default
        # so the sync attempt doesn't crash the whole ROM loop.
        _logger.warning(
            "Sanitized server-supplied save target — invalid file_extension=%r; falling back to 'srm'",
            ext,
        )
        return f"{rom_name}.srm"
    if sanitized != target:
        # Path-traversal characters were stripped (e.g. ``../etc/passwd``).
        _logger.warning(
            "Sanitized server-supplied save target from %r to %r (file_extension=%r)",
            target,
            sanitized,
            ext,
        )
    return sanitized
