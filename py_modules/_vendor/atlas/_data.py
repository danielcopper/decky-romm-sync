"""Read packaged world knowledge from this package — whatever this package is called.

Vendoring is a directory copy (the zero-dependency contract, ``pyproject.toml``),
and the copy's owner chooses the parent: a host that keeps foreign code inside
its own package imports the copy as ``_vendor.atlas``, not ``atlas``. A data read
anchored to the literal name ``"atlas"`` breaks exactly there — the name
addresses whatever package the host calls ``atlas``, not the copy doing the
reading. ``__package__`` inside this module *is* the copy's own dotted name
under whatever parent the host chose, so anchoring here addresses the copy
itself (issue #327).

Every packaged table under ``atlas/data/`` is read through this one function,
so the next data file inherits the anchor instead of re-deciding it.
"""

from __future__ import annotations

import importlib.resources

# __package__ of a submodule, in the form the type checker accepts (typeshed
# declares ``__package__`` as ``str | None``; ``__name__`` is always ``str``).
_PACKAGE = __name__.rpartition(".")[0]


def packaged_text(name: str) -> str:
    """The UTF-8 text of the packaged data file ``data/<name>``."""
    return importlib.resources.files(_PACKAGE).joinpath("data", name).read_text(encoding="utf-8")
