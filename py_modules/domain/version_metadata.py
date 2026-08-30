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

    ``sibling_group_key`` names the sibling group this dump belongs to and has
    exactly two legal states: a real key (``"{source}:{value}:{platform}"``, or
    the ``romm:{rom_id}:{platform_id}`` solo fallback) or ``None`` — no group
    derived yet. ``regions`` / ``languages`` / ``revision`` / ``tags`` describe
    how the dump differs from its siblings; ``is_main_sibling`` marks the group's
    representative dump. The defaults are the "no version metadata known" state —
    an empty instance is the neutral value the ROM factory falls back to.
    """

    sibling_group_key: str | None = None
    regions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    revision: str = ""
    tags: tuple[str, ...] = ()
    is_main_sibling: bool = False

    def __post_init__(self) -> None:
        """Reject an empty-string ``sibling_group_key``.

        The empty string names no state, and raises ``ValueError`` — "no group
        derived yet" stays ``None`` (unknown), never an empty string. Exactly
        that one value: no format or whitespace rule is applied, so a key is
        stored as given. This is the only route by which a **new** key enters
        the ``Rom`` aggregate (loading a stored row via
        ``adapters.repositories.rom._row_to_rom`` validates nothing), so
        guarding the write direction here is what lets the residency readers ask
        ``is not None`` and mean it.
        """
        if self.sibling_group_key == "":
            raise ValueError("sibling_group_key must not be empty")

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any], *, sibling_group_key: str | None = None) -> VersionMetadata:
        """Build a ``VersionMetadata`` from a flat RomM-derived mapping.

        Applies the same safe defaulting the sync and version-switch persist
        paths use (``.get(...) or default``), so a missing or ``null`` field
        degrades to its neutral value rather than raising. *sibling_group_key*
        overrides the mapping's key when it is a real key (the version-switch
        case, where the target adopts its bound group's key); an empty or missing
        override falls back to the mapping's own key. Only when neither side
        carries a key does it degrade to ``None``, rather than tripping the
        empty-key invariant.
        """
        return cls(
            sibling_group_key=sibling_group_key or m.get("sibling_group_key") or None,
            regions=tuple(m.get("regions") or ()),
            languages=tuple(m.get("languages") or ()),
            revision=m.get("revision") or "",
            tags=tuple(m.get("tags") or ()),
            is_main_sibling=bool(m.get("is_main_sibling", False)),
        )
