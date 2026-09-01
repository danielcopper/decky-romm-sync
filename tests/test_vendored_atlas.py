"""The vendored emu-atlas copy resolves and is the release we pinned.

``scripts/check_vendored_atlas.py`` proves the bytes on disk are the tagged
release's; this proves the copy is a working import under ``_vendor.atlas``
rather than a tree that merely hashes correctly. Both halves are needed: upstream
made the package relocatable (no absolute self-imports, no ``files("atlas")``),
so it is vendored with no local patches, and an upstream change that broke that
property would be invisible to a checksum.

Deliberately not a conformance suite. emu-atlas is tested upstream and the whole
point of pinning a release is that we do not re-test a vendored dependency.
"""

from __future__ import annotations

from _vendor import atlas

VENDORED_VERSION = "0.5.0"


def test_vendored_atlas_is_the_pinned_release() -> None:
    assert atlas.__version__ == VENDORED_VERSION
