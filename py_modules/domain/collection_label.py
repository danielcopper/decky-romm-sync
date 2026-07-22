"""Fine display label for a RomM collection, keyed by kind + virtual type.

The pure kernel behind the ``by_label`` Steam-collection naming mode: it maps a
collection's ``kind`` (``standard`` / ``smart`` / ``virtual``) and, for the
virtual kind, its ``virtual_type`` (``franchise`` / ``collection``) onto the
short human label the reporter appends to a collection name so same-named
collections of different types stay separate Steam collections
(``RomM: [<name> (Franchise)] (host)``). No I/O, no state.

The label strings MUST match the frontend vocabulary exactly — ``SUB_TAB_LABELS``
and ``VIRTUAL_TYPE_LABELS`` in ``src/components/LibraryPage.tsx`` — so the type a
user sees on the Collections page is the type baked into the Steam name. Keep the
two in sync.

Label-format safety: a label is appended inside the single bracket pair of
``RomM: [<name> (<label>)]``, and the frontend reconcile parses that name with
``/^RomM: \\[([^\\]]+)\\]/`` (``src/index.tsx``). So a label must contain **no**
``]`` character (parens are safe, brackets would truncate the parsed name and
orphan the collection). Every label below is bracket-free; the fallback
capitalises a controlled kind literal, which is too.
"""

from __future__ import annotations

# Coarse kind → label. Mirrors ``SUB_TAB_LABELS`` in LibraryPage.tsx.
_KIND_LABELS: dict[str, str] = {
    "standard": "Standard",
    "smart": "Smart",
    "virtual": "Virtual",
}

# Virtual sub-type → label. Mirrors ``VIRTUAL_TYPE_LABELS`` in LibraryPage.tsx:
# "IGDB Collection" (not "Collection") disambiguates from the Collections page
# it lives inside.
_VIRTUAL_TYPE_LABELS: dict[str, str] = {
    "franchise": "Franchise",
    "collection": "IGDB Collection",
}


def collection_label(kind: str, virtual_type: str | None) -> str:
    """Return the fine display label for a collection of ``kind`` / ``virtual_type``.

    ``standard`` → ``"Standard"``, ``smart`` → ``"Smart"``. A ``virtual``
    collection resolves to its virtual-type label — ``franchise`` →
    ``"Franchise"``, ``collection`` → ``"IGDB Collection"`` — falling back to
    ``"Virtual"`` when the type is missing or unrecognised (an older backend, a
    virtual type the plugin does not sync). An unknown ``kind`` degrades to the
    capitalised kind string (never expected for the ``standard`` / ``smart`` /
    ``virtual`` set), and an empty kind to ``"Collection"``. Every branch is
    free of ``]`` so the reconcile name-parse stays intact.
    """
    if kind == "virtual":
        if virtual_type is not None and virtual_type in _VIRTUAL_TYPE_LABELS:
            return _VIRTUAL_TYPE_LABELS[virtual_type]
        return _KIND_LABELS["virtual"]
    if kind in _KIND_LABELS:
        return _KIND_LABELS[kind]
    return kind.capitalize() if kind else "Collection"
