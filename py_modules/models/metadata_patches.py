"""Typed mutation records for ``metadata_cache[rom_id_str]``.

The metadata cache is single-writer — only the metadata service writes
entries — so the patch surface is narrow. Patches keep the cache writer
aligned with the typed ``MetadataCacheEntry`` shape and give the store
a uniform call site.

This module is data-only: no I/O, no service or adapter imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.state import MetadataCacheEntry


@dataclass(frozen=True)
class MetadataStampPatch:
    """Replace ``metadata_cache[rom_id_str]`` with a freshly-built entry.

    Owner: metadata service. ``entry`` is the full ``MetadataCacheEntry``
    payload — the store assigns it as-is rather than merging.
    """

    rom_id_str: str
    entry: MetadataCacheEntry
