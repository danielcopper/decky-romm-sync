"""Tests for ``domain/disc_formats`` — the disc-image extension constant.

Pins the irreducible disc set and asserts the downloads m3u collector still
derives its disc-suffix filter from this single constant (no inline drift).
"""

from __future__ import annotations

import inspect

from domain.disc_formats import DISC_IMAGE_EXTENSIONS


class TestDiscImageExtensions:
    def test_is_the_irreducible_disc_set(self):
        assert frozenset({".cue", ".chd", ".iso"}) == DISC_IMAGE_EXTENSIONS

    def test_is_a_frozenset(self):
        assert isinstance(DISC_IMAGE_EXTENSIONS, frozenset)

    def test_all_extensions_are_lowercase_with_leading_dot(self):
        for ext in DISC_IMAGE_EXTENSIONS:
            assert ext.startswith(".")
            assert ext == ext.lower()

    def test_sidecar_and_playlist_excluded(self):
        # .bin (a .cue's sidecar) and .m3u (a playlist) are NOT disc units.
        assert ".bin" not in DISC_IMAGE_EXTENSIONS
        assert ".m3u" not in DISC_IMAGE_EXTENSIONS


class TestDownloadsCollectorUsesConstant:
    """The downloads m3u collector must filter on the constant, not an inline glob."""

    def test_collector_imports_the_constant(self):
        from services import downloads

        # The module imports the shared constant rather than redefining the set.
        assert downloads.DISC_IMAGE_EXTENSIONS is DISC_IMAGE_EXTENSIONS

    def test_collector_source_filters_on_the_constant(self):
        from services import downloads

        source = inspect.getsource(downloads.DownloadService._maybe_generate_m3u_io)
        # The disc filter is derived from the constant (a suffix tuple of it),
        # and the old inline tuple literal is gone.
        assert "DISC_IMAGE_EXTENSIONS" in source
        assert '(".cue", ".chd", ".iso")' not in source
