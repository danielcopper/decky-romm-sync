"""RetroArch save-file layout value objects.

The single source of truth for *where* RetroArch writes save files, as
observed from ``retroarch.cfg``. The ``SaveLayout`` union models the two
mutually-exclusive states the plugin must distinguish: saves under the
RetroDECK saves root (``InSaveDir``, the supported case whose two flags
pick the subdirectory layout) versus saves written next to the ROM
(``ContentDir``, ``savefiles_in_content_dir=true`` — the case where
plugin save sync is impossible because the files live outside the saves
tree the plugin scans).

Pure value objects only — no I/O, no parsing. The adapter that reads
``retroarch.cfg`` produces these; services pattern-match on them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InSaveDir:
    """Saves live under the RetroDECK saves root; the two flags pick the subdir layout."""

    sort_by_content: bool
    sort_by_core: bool


@dataclass(frozen=True, slots=True)
class ContentDir:
    """RetroArch savefiles_in_content_dir=true: saves live next to the ROM — unsupported."""


SaveLayout = InSaveDir | ContentDir

# Canonical ``reason`` slug for the benign-skip outcome a ``ContentDir`` layout
# produces. Lives here as the single source of truth so every service routes on
# the SAME value without a service→service import: the saves sync-engine gate
# stamps it on its skip result and the session-lifecycle post-exit branch reads
# it to suppress the false-failure toast (#239).
SAVE_SYNC_CONTENT_DIR_REASON = "savefiles_in_content_dir"
