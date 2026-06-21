"""Property-based tests for ``domain/disc_selection`` (#865).

Two safety invariants the disc picker must never break:

1. **Containment** — ``resolve_launch_path`` never returns a path the caller
   didn't supply: the result is always either the install's ``file_path`` or
   one of the enumerated discs' paths. A disc selection is a bake-time
   *path-override*, so an out-of-set path would point launch_options at a file
   that doesn't belong to this ROM.
2. **Order-independence of the default** — with no selection, the chosen
   default (m3u or disc 1) is a function of the file *set*, not the order the
   filesystem happened to list them in. Permuting the input file order must not
   change the resolved default path.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from domain.disc_formats import DISC_IMAGE_EXTENSIONS
from domain.disc_selection import enumerate_discs, resolve_launch_path

_DIR = "/roms/psx/Game"
_EXTENSIONS = sorted(DISC_IMAGE_EXTENSIONS)

# Basenames drawn from a small space: a disc-number tag (parseable) or a plain
# stem (unparseable), each carrying a disc-image extension. Includes a couple of
# non-disc files so the enumeration filter is exercised too.
_disc_names = st.builds(
    lambda stem, num, ext: f"{stem} (Disc {num}){ext}",
    st.sampled_from(["Game", "FF7", "Chrono"]),
    st.integers(min_value=1, max_value=12),
    st.sampled_from(_EXTENSIONS),
)
_plain_names = st.builds(
    lambda stem, ext: f"{stem}{ext}",
    st.sampled_from(["alpha", "beta", "gamma"]),
    st.sampled_from(_EXTENSIONS),
)
_non_disc_names = st.sampled_from(["readme.txt", "Game.bin", "Game.m3u", "cover.png"])

_file_names = st.lists(
    st.one_of(_disc_names, _plain_names, _non_disc_names),
    min_size=0,
    max_size=8,
    unique=True,
)


def _paths(names: list[str]) -> list[str]:
    return [f"{_DIR}/{name}" for name in names]


@given(names=_file_names, file_path=st.sampled_from([f"{_DIR}/Game.m3u", f"{_DIR}/Game (Disc 1).cue"]), pin=st.text())
def test_resolve_never_returns_path_outside_inputs(names: list[str], file_path: str, pin: str):
    files = _paths(names)
    discs = enumerate_discs(files, None)
    allowed = {file_path, *(d.path for d in discs)}

    # Try both no-selection and an arbitrary (often stale) pin.
    for selected in (None, pin):
        path, _stale = resolve_launch_path(file_path, discs, selected)
        assert path in allowed


@given(names=st.lists(_disc_names, min_size=2, max_size=8, unique=True), data=st.data())
def test_default_is_order_independent(names: list[str], data: st.DataObject):
    files = _paths(names)
    # A reference file_path that is one of the (sorted) candidate discs so the
    # default is disc 1, plus the m3u variant — both must be order-stable.
    for file_path in (f"{_DIR}/Game.m3u", f"{_DIR}/{names[0]}"):
        baseline_discs = enumerate_discs(files, None)
        baseline_path, _ = resolve_launch_path(file_path, baseline_discs, None)

        shuffled = data.draw(st.permutations(files))
        shuffled_discs = enumerate_discs(list(shuffled), None)
        shuffled_path, _ = resolve_launch_path(file_path, shuffled_discs, None)

        assert shuffled_path == baseline_path
