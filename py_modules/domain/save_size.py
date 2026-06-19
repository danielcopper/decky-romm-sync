"""Plausibility checks on a local save file's size before it overwrites a server copy.

A crashed emulator or a full disk can leave a 0-byte or truncated save behind. Such a
file is still a valid regular file with a valid (but wrong) content hash, so the
newest-wins kernel would treat it as a legitimate offline edit and PUT it over the only
good server copy — which RomM updates in place (versions only on POST, never on PUT). The
predicates here let the kernel refuse that in-place overwrite and route the case to a
user-resolved conflict instead.

No I/O, no service/adapter/lib imports. Pure functions only.
"""

from __future__ import annotations

# Maintainer-tunable safety threshold: a local save that has shrunk to below this
# fraction of its recorded baseline size is treated as implausible (likely a partial /
# truncated write). Not configurable at runtime — a dumb, conservative default.
_DEFAULT_SHRINK_RATIO = 0.5


def is_implausibly_shrunken(
    new_size: int | None,
    baseline_size: int | None,
    *,
    shrink_ratio: float = _DEFAULT_SHRINK_RATIO,
) -> bool:
    """Return True when *new_size* looks like a corrupt / truncated save.

    Two independent triggers:

    - *new_size* is exactly ``0`` — an empty save is never a plausible edit of a
      real save, so this fires unconditionally (no baseline required).
    - a positive *baseline_size* exists and *new_size* has dropped below
      ``baseline_size * shrink_ratio`` — a dramatic shrink that signals a partial
      write rather than a normal edit.

    Returns False when *new_size* is ``None`` (unknown — nothing to judge), and
    when there is no positive baseline to compare a non-empty *new_size* against
    (a first sync, or a baseline of 0/None, never blocks a non-empty save).
    """
    if new_size is None:
        return False
    if new_size == 0:
        return True
    if baseline_size is None or baseline_size <= 0:
        return False
    return new_size < baseline_size * shrink_ratio
