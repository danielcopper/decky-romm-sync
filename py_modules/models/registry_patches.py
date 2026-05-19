"""Typed mutation records for ``shortcut_registry[rom_id_str]``.

Every writer that touches the registry expresses its intent as one of
these frozen dataclasses. The patch type carries exactly the fields the
writer is allowed to change — the registry store applies the patch,
preserves all other fields on the row, and the type checker rejects any
attempt to widen a writer's surface beyond what its patch owns.

This module is data-only: no I/O, no service or adapter imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegistrySyncApplyPatch:
    """Full-row write at sync-apply commit.

    Owner: sync apply. Required fields rebuild the registry row from
    scratch; optional ID fields are ``None`` to mean "preserve whatever
    the existing row carries" and a concrete value to mean "set it".
    """

    rom_id_str: str
    app_id: int
    name: str
    fs_name: str
    platform_name: str
    platform_slug: str
    cover_path: str
    igdb_id: int | None = None
    sgdb_id: int | None = None
    ra_id: int | None = None


@dataclass(frozen=True)
class RegistryCoverPathPatch:
    """Refresh of ``cover_path`` only.

    Owner: artwork refresh. The store no-ops on a missing row — a stale
    patch for a deleted shortcut is expected, not an error.
    """

    rom_id_str: str
    cover_path: str


@dataclass(frozen=True)
class RegistrySgdbIdPatch:
    """Write of ``sgdb_id`` only.

    Owner: SteamGridDB lazy lookup. The store no-ops on a missing row.
    """

    rom_id_str: str
    sgdb_id: int


@dataclass(frozen=True)
class RegistryIdsPatch:
    """Write of ``sgdb_id`` and/or ``igdb_id``.

    Owner: SteamGridDB RomM-API fetch. ``None`` on either field means
    "leave that field alone"; if both are ``None`` the call is a no-op.
    """

    rom_id_str: str
    sgdb_id: int | None
    igdb_id: int | None


@dataclass(frozen=True)
class RegistryDeletePatch:
    """Removal of the row.

    Owner: shortcut removal and startup healing.
    """

    rom_id_str: str
