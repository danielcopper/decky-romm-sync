"""Uploader attribution for server saves.

Three-way ``uploaded_by_us`` flag derived from RomM server-save records
and the per-ROM ``own_upload_ids`` list. Pure: no I/O, no logging.
"""

from __future__ import annotations

from typing import Any


def compute_uploaded_by_us(
    server_save: dict[str, Any] | None,
    own_upload_ids: list[int] | None,
) -> bool | None:
    """Three-way attribution: True/False when ``own_upload_ids`` is known
    for this ROM (we can tell whether this installation POSTed the save);
    None for legacy ROM state without the ``own_upload_ids`` field
    (attribution unknown). Returns None when *server_save* or its ``id``
    is missing too — there is nothing to attribute.
    """
    if server_save is None or own_upload_ids is None:
        return None
    sid = server_save.get("id")
    if sid is None:
        return None
    return sid in own_upload_ids
