"""Unit tests for ``domain.save_size.is_implausibly_shrunken``.

Pins the plausibility-guard decision (#1062): a 0-byte or implausibly-shrunken
local save must be flagged so the sync kernel refuses an in-place PUT over the
only good server copy. Pure-domain only — no I/O.
"""

from __future__ import annotations

from domain.save_size import _DEFAULT_SHRINK_RATIO, is_implausibly_shrunken


class TestZeroByteGate:
    """A 0-byte local save is never plausible — the unconditional trigger."""

    def test_zero_with_baseline_is_implausible(self):
        assert is_implausibly_shrunken(0, 8192) is True

    def test_zero_with_no_baseline_is_implausible(self):
        assert is_implausibly_shrunken(0, None) is True

    def test_zero_with_zero_baseline_is_implausible(self):
        assert is_implausibly_shrunken(0, 0) is True


class TestShrinkGate:
    """A dramatic shrink versus a positive baseline is implausible."""

    def test_just_below_half_is_implausible(self):
        # 4095 < 8192 * 0.5 (4096) → fires.
        assert is_implausibly_shrunken(4095, 8192) is True

    def test_exactly_half_is_plausible(self):
        # 4096 is not < 8192 * 0.5 (4096) — boundary is NOT a shrink.
        assert is_implausibly_shrunken(4096, 8192) is False

    def test_just_above_half_is_plausible(self):
        assert is_implausibly_shrunken(4097, 8192) is False

    def test_far_below_half_is_implausible(self):
        assert is_implausibly_shrunken(10, 8192) is True

    def test_larger_than_baseline_is_plausible(self):
        # Growth is always a plausible edit.
        assert is_implausibly_shrunken(16384, 8192) is False

    def test_equal_to_baseline_is_plausible(self):
        assert is_implausibly_shrunken(8192, 8192) is False


class TestNoBaseline:
    """Without a positive baseline a non-empty save can't be judged a shrink."""

    def test_none_baseline_with_nonzero_size_is_plausible(self):
        assert is_implausibly_shrunken(1, None) is False

    def test_zero_baseline_with_nonzero_size_is_plausible(self):
        assert is_implausibly_shrunken(1, 0) is False

    def test_negative_baseline_treated_as_no_baseline(self):
        # A garbage negative baseline must not yield a bogus threshold.
        assert is_implausibly_shrunken(1, -100) is False


class TestUnknownSize:
    """An unknown (None) new size cannot be judged — never flag it."""

    def test_none_size_with_baseline_is_plausible(self):
        assert is_implausibly_shrunken(None, 8192) is False

    def test_none_size_with_no_baseline_is_plausible(self):
        assert is_implausibly_shrunken(None, None) is False


class TestCustomRatio:
    """The threshold is overridable for callers that need a different cutoff."""

    def test_custom_ratio_changes_threshold(self):
        # With ratio 0.9, 5000 < 8192 * 0.9 (7372.8) → fires, even though it
        # passes the default 0.5 gate.
        assert is_implausibly_shrunken(5000, 8192) is False
        assert is_implausibly_shrunken(5000, 8192, shrink_ratio=0.9) is True

    def test_default_ratio_is_half(self):
        assert _DEFAULT_SHRINK_RATIO == 0.5
