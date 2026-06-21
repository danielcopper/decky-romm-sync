"""Disc-image container formats — the irreducible hardcoded disc set.

The file extensions that denote a launchable disc *image* (a single-disc
container the emulator opens directly), as opposed to a sidecar owned by
another file (``.bin``, referenced by its ``.cue``) or a playlist that
points at several discs (``.m3u``). This knowledge is format-semantic and
emulator-independent: it answers "is this file shape a disc image?", which
``es_systems.xml`` cannot — that file is a flat per-system accept-list with
no per-token role metadata, so it can say a system *accepts* ``.cue`` but
never that ``.cue`` is a disc while ``.bin`` is its sidecar.

Membership here is the disc *unit* itself (the ``.cue``/``.chd``/``.iso``),
never the ``.bin`` a ``.cue`` references. The live per-system capability list
(which of these a given emulator can actually launch) is read from
es_systems and intersected with this set at enumeration time — it is NOT
hardcoded here. See ADR-0014.

No I/O, no service/adapter/lib imports. A constant only.
"""

from __future__ import annotations

DISC_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".cue", ".chd", ".iso"})
