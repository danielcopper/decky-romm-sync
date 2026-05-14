"""Logging wrappers around pure domain helpers consumed by the saves package."""

from __future__ import annotations

import logging

from domain.save_path import compute_local_save_target

_logger = logging.getLogger(__name__)


def _local_save_target(server_save: dict, rom_name: str) -> str:
    """Resolve the local filename for *server_save*, logging any sanitization."""
    result = compute_local_save_target(server_save, rom_name)
    if result.fallback_extension is not None:
        _logger.warning(
            "Sanitized server-supplied save target — invalid file_extension=%r; falling back to 'srm'",
            result.fallback_extension,
        )
    elif result.sanitized_from is not None:
        _logger.warning(
            "Sanitized server-supplied save target from %r to %r (file_extension=%r)",
            result.sanitized_from,
            result.filename,
            server_save.get("file_extension", "srm"),
        )
    return result.filename
