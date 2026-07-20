"""VersionMetadata — the ADR-0021 server-derived version facts as one value.

The sibling-group identity and version dimensions RomM supplies per ROM: which
sibling group a dump belongs to, and how it differs from its siblings
(region/language/revision/tag variants, and whether it is the group's main
sibling). Bundled here so the cohesive set travels as a single value object
across the factory boundary rather than as loose parallel parameters.

Pure value object — no I/O, no service/adapter imports. The ``Rom`` aggregate
keeps these as flat fields; this type is the parameter object its factory
accepts, and the shape :func:`domain.shortcut_data.extract_version_metadata`
already treats as a unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class VersionMetadata:
    """The server-derived version facts for one ROM (ADR-0021).

    ``sibling_group_key`` names the sibling group this dump belongs to;
    ``regions`` / ``languages`` / ``revision`` / ``tags`` describe how it differs
    from its siblings; ``is_main_sibling`` marks the group's representative dump.
    The defaults are the "no version metadata known" state — an empty instance is
    the neutral value the ROM factory falls back to.
    """

    sibling_group_key: str | None = None
    regions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    revision: str = ""
    tags: tuple[str, ...] = ()
    is_main_sibling: bool = False

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any], *, sibling_group_key: str | None = None) -> VersionMetadata:
        """Build a ``VersionMetadata`` from a flat RomM-derived mapping.

        Applies the same safe defaulting the sync and version-switch persist
        paths use (``.get(...) or default``), so a missing or ``null`` field
        degrades to its neutral value rather than raising. *sibling_group_key*
        overrides the mapping's key when truthy (the version-switch case, where
        the target adopts its bound group's key); a falsy override falls back to
        the mapping's own key.
        """
        return cls(
            sibling_group_key=sibling_group_key or m.get("sibling_group_key"),
            regions=tuple(m.get("regions") or ()),
            languages=tuple(m.get("languages") or ()),
            revision=m.get("revision") or "",
            tags=tuple(m.get("tags") or ()),
            is_main_sibling=bool(m.get("is_main_sibling", False)),
        )
