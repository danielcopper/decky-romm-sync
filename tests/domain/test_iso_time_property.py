"""Property-based tests for ISO-8601 epoch parsing (#1028).

Invariant 3 sharpener (#1014 — lexicographic timestamp ordering): the
save-sync kernels order server saves by ``parse_iso_to_epoch`` rather than by
raw string comparison. This module pins the underlying parse: two ISO forms
that denote the *same instant* — ``…Z`` vs ``…+00:00`` vs
``….000000+00:00`` — must parse to the same epoch, so any ``max``/sort keyed
on the epoch is stable under format variation. The #1014 bug is the opposite:
sites that compared the raw strings mis-ordered mixed-shape timestamps.

See ``tests.domain.test_sync_action_property`` for the convention note on the
``xfail(strict=True)`` pinning of properties that encode an open bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from domain.iso_time import parse_iso_to_epoch

# Aware UTC datetimes across a wide range; whole-second and microsecond
# resolution both exercised by the formatting helper below.
_utc_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2099, 12, 31, 23, 59, 59),
    timezones=st.just(UTC),
)


def _iso_forms(dt: datetime) -> list[str]:
    """All semantically-equal ISO renderings of a UTC instant the kernels see.

    RomM has emitted ``updated_at`` with a trailing ``Z``, with an explicit
    ``+00:00`` offset, and with/without microseconds across versions. These
    must all parse to one epoch.
    """
    whole = dt.replace(microsecond=0)
    base = whole.strftime("%Y-%m-%dT%H:%M:%S")
    micros = f"{base}.000000"
    return [
        f"{base}+00:00",
        f"{base}Z",
        f"{micros}+00:00",
        f"{micros}Z",
    ]


@given(dt=_utc_datetimes)
def test_equivalent_iso_forms_parse_to_same_epoch(dt: datetime) -> None:
    """Every semantically-equal ISO rendering of an instant parses to the same
    epoch. A ``max``/sort keyed on the epoch is therefore order-stable under
    format variation — the property the #1014 sites violated by comparing raw
    strings.
    """
    epochs = {parse_iso_to_epoch(form) for form in _iso_forms(dt)}
    assert len(epochs) == 1
    (only,) = epochs
    assert only is not None


@given(earlier=_utc_datetimes, later=_utc_datetimes)
def test_epoch_ordering_independent_of_format(earlier: datetime, later: datetime) -> None:
    """Ordering by parsed epoch reflects true chronology regardless of the ISO
    *shape* each side is rendered in. Picks the ``Z`` form for the earlier and
    the ``+00:00`` form for the later instant — the exact mixed-shape pairing
    a lexicographic string compare can invert.
    """
    e0 = earlier.replace(microsecond=0)
    l0 = later.replace(microsecond=0)
    earlier_str = e0.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    later_str = l0.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"

    epoch_earlier = parse_iso_to_epoch(earlier_str)
    epoch_later = parse_iso_to_epoch(later_str)
    assert epoch_earlier is not None
    assert epoch_later is not None

    # Epoch ordering must match true instant ordering.
    assert (epoch_earlier <= epoch_later) == (e0 <= l0)
