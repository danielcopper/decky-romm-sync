"""Property-based tests for domain.save_hash.combine_zip_entry_hashes (#1234 1b).

The combined zip content hash is convergence-critical: it must match RomM's
``_compute_zip_hash`` byte-for-byte or a zipped save round-trips between client
and server forever. Two invariants the generated input space exercises better
than the hand-enumerated cases in ``test_save_hash``:

- **Permutation-independence**: the result depends only on the *set* of
  ``(name, digest)`` entries, never on the order they were read from the
  archive — because the function sorts by name before joining. A regression
  that dropped the sort would still pass the happy-path example but break this.
- **Output shape**: the result is always a 32-char lowercase MD5 hex digest.

The CI-safe Hypothesis profile (``deadline=None``, fixed ``max_examples``) is
applied in ``tests/conftest.py``.
"""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from domain.save_hash import combine_zip_entry_hashes

# Realistic zip-entry names (unique via dict keys) and 32-char MD5 hex digests.
_NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-/", min_size=1, max_size=16)
_HEX = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
_ENTRY_SETS = st.dictionaries(keys=_NAMES, values=_HEX, max_size=8)


@given(entries=_ENTRY_SETS, data=st.data())
def test_permutation_independent(entries, data):
    items = list(entries.items())
    permuted = list(data.draw(st.permutations(items)))
    assert combine_zip_entry_hashes(items) == combine_zip_entry_hashes(permuted)


@given(entries=_ENTRY_SETS)
def test_output_is_md5_hex(entries):
    result = combine_zip_entry_hashes(list(entries.items()))
    assert re.fullmatch(r"[0-9a-f]{32}", result)


@given(entries=_ENTRY_SETS)
def test_deterministic(entries):
    items = list(entries.items())
    first = combine_zip_entry_hashes(items)
    second = combine_zip_entry_hashes(items)
    assert first == second
