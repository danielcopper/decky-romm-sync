"""The vendored emu-atlas copy resolves and is the release we pinned.

``scripts/check_vendored_trees.py`` proves the bytes on disk are the tagged
release's; this proves the copy is a working import under ``_vendor.atlas``
rather than a tree that merely hashes correctly. Both halves are needed: upstream
made the package relocatable (no absolute self-imports, no ``files("atlas")``),
so it is vendored with no local patches, and an upstream change that broke that
property would be invisible to a checksum.

The import is proven twice, and the second time is the one a checksum and a
plain import both miss. Decky Loader's PyInstaller runtime ships only the
modules its build analysis reached, and ``xml.etree`` is not among them:
``import xml.etree.ElementTree`` raises ``ModuleNotFoundError`` there while the
expat extension it is written over sits in the bundle's ``lib-dynload``. Upstream
answers that with :mod:`_vendor.atlas._xml`, ElementTree's shape on expat
directly — but nothing about a release states it, so a future one reaching for
``xml.etree`` again would import cleanly in CI and kill the backend at bootstrap
on a real Deck. Blocking the module at ``sys.meta_path`` is the cheapest stand-in
for that runtime.

Deliberately not a conformance suite. emu-atlas is tested upstream and the whole
point of pinning a release is that we do not re-test a vendored dependency.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest
from _vendor import atlas

if TYPE_CHECKING:
    from types import ModuleType

VENDORED_VERSION = "0.12.0"


class _NoEtree:
    """A meta-path finder that refuses ``xml.etree``, as the frozen runtime does."""

    def find_spec(self, fullname: str, path: Any = None, target: ModuleType | None = None) -> None:
        if fullname == "xml.etree" or fullname.startswith("xml.etree."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return


def test_vendored_atlas_is_the_pinned_release() -> None:
    assert atlas.__version__ == VENDORED_VERSION


def test_the_vendored_copy_imports_and_parses_without_xml_etree(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [module for module in sys.modules if module == "xml.etree" or module.startswith("xml.etree.")]:
        monkeypatch.delitem(sys.modules, name)
    for name in [module for module in sys.modules if module == "_vendor.atlas" or module.startswith("_vendor.atlas.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoEtree(), *sys.meta_path])

    with pytest.raises(ModuleNotFoundError):
        __import__("xml.etree.ElementTree")

    from _vendor import atlas as reimported
    from _vendor.atlas import _xml

    assert reimported.__version__ == VENDORED_VERSION
    # Direct children only — the vendored parser rebuilds ElementTree's surface
    # without its path expressions, so "system/name" would answer None here.
    parsed = _xml.fromstring("<systemList><system><name>snes</name></system></systemList>")
    system = parsed.find("system")
    assert system is not None
    assert system.findtext("name") == "snes"
