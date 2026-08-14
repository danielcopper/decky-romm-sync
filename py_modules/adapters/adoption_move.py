"""Filesystem adapter for carrying an adopted ROM and its saves to canonical names.

Owns the only multi-file mutation the adoption performs, and the reads that plan
it. A set of five renames is five syscalls and no kernel renames them as one, so
this adapter's whole job is to put the failure somewhere harmless and to report
precisely where it landed when it could not.

It deliberately owns no deletion. An adoption's Overwrite destroys save files,
and those go through the sanctioned ``.romm-backup`` funnel — a second way to
destroy a save here is how the first one stops being the discipline (#965).

Spans three RetroDECK trees — ``roms``, ``saves`` and ``states`` — which is why
it is its own seam rather than a method on the ROM or the save file store.
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.adoption import MoveOutcome

# Errnos that mean "this filesystem will not give me a second name for these
# bytes", as opposed to "this particular link failed". ``EXDEV`` is the ordinary
# one — a ROM library on removable storage with saves on internal storage is a
# common layout — while ``EPERM``/``EOPNOTSUPP``/``EMLINK`` cover a filesystem
# that refuses hardlinks outright. Only these fall through to the rename path;
# anything else is a failure to report with nothing moved.
_LINK_UNSUPPORTED = frozenset({errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK, errno.EISDIR})


class AdoptionMoveAdapter:
    """Synchronous filesystem operations for an adoption's rename.

    Implements the ``AdoptionMoveStore`` Protocol. Methods are synchronous —
    services that call from an async context offload via ``loop.run_in_executor``.
    """

    def list_names(self, directory: str) -> tuple[str, ...]:
        """Return the file names directly inside *directory*; empty when it does not exist.

        Files only: a save directory's subdirectories belong to another content
        group, and the rename never descends into one.
        """
        try:
            with os.scandir(directory) as entries:
                return tuple(entry.name for entry in entries if entry.is_file())
        except OSError:
            return ()

    def exists(self, path: str) -> bool:
        """Return True when *path* exists, a dangling symlink included.

        ``lexists`` rather than ``exists``: a broken link still occupies the name,
        and a move onto it would fail — so for collision purposes it is taken.
        """
        return os.path.lexists(path)

    def is_file(self, path: str) -> bool:
        """Return True when *path* is a regular file, following symlinks."""
        return os.path.isfile(path)

    def move_pairs(self, pairs: tuple[tuple[str, str], ...]) -> MoveOutcome:
        """Carry every ``(source, target)`` pair, keeping a failure recoverable.

        Where the filesystem allows it, **link then unlink**: a second name is
        made for every pair before a single original is removed, so a failure
        during staging is undone by dropping the links and the state is exactly
        as it started. Only once every link exists are the originals unlinked,
        and a failure there leaves two names for one inode — no data lost, and a
        re-run finishes it.

        Hardlinks need one filesystem and cannot name a directory at all, so a
        set containing a directory, or one spanning a mount boundary, falls back
        to rename-with-rollback. That path can leave a genuinely partial state,
        which is reported by name — never as success and never as a plain
        failure.
        """
        if not pairs:
            return {"moved": [], "stranded": [], "unmoved": [], "error": ""}
        parent_error = self._ensure_parents(pairs)
        if parent_error:
            return {"moved": [], "stranded": [], "unmoved": [source for source, _ in pairs], "error": parent_error}
        if any(os.path.isdir(source) and not os.path.islink(source) for source, _ in pairs):
            return self._rename_with_rollback(pairs)
        return self._link_then_unlink(pairs)

    @staticmethod
    def _ensure_parents(pairs: tuple[tuple[str, str], ...]) -> str:
        """Create every target's parent directory. Returns an error string, or ``""``.

        Runs before anything is staged: a directory that cannot be created is a
        reason to move nothing, and finding that out halfway through would leave
        the set split across two names.
        """
        for _source, target in pairs:
            parent = os.path.dirname(target)
            if not parent:
                continue
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return f"could not create {parent}: {e}"
        return ""

    def _link_then_unlink(self, pairs: tuple[tuple[str, str], ...]) -> MoveOutcome:
        """Stage every pair as a hardlink, then remove the originals."""
        staged: list[str] = []
        for source, target in pairs:
            try:
                os.link(source, target)
            except OSError as e:
                leftovers = self._drop_links(staged)
                if e.errno in _LINK_UNSUPPORTED:
                    return self._rename_with_rollback(pairs, leftovers)
                return {
                    "moved": [],
                    "stranded": [],
                    "unmoved": [pair_source for pair_source, _ in pairs],
                    "error": _describe_failure(source, e, leftovers),
                }
            staged.append(target)

        moved: list[str] = []
        stranded: list[str] = []
        problems: list[str] = []
        for source, target in pairs:
            try:
                os.unlink(source)
            except OSError as e:
                stranded.append(source)
                problems.append(f"{os.path.basename(source)}: {e}")
            else:
                moved.append(target)
        return {
            "moved": moved,
            "stranded": stranded,
            "unmoved": [],
            "error": (
                "the files are all in place, but the old copies could not be removed: " + "; ".join(problems)
                if problems
                else ""
            ),
        }

    @staticmethod
    def _drop_links(staged: list[str]) -> list[str]:
        """Remove the links made so far; return the ones that could not be removed."""
        leftovers: list[str] = []
        for target in reversed(staged):
            try:
                os.unlink(target)
            except OSError:
                leftovers.append(target)
        return leftovers

    def _rename_with_rollback(
        self, pairs: tuple[tuple[str, str], ...], leftovers: list[str] | None = None
    ) -> MoveOutcome:
        """Rename each pair in turn, undoing the ones already done if one fails.

        The fallback for a set hardlinks cannot express. Each target is probed
        immediately before its rename because ``os.rename`` replaces whatever is
        there without a word, and Python exposes no ``RENAME_NOREPLACE``: the
        probe narrows that window rather than closing it, which is the honest
        description of what it buys.

        *leftovers* are links a staging attempt made and could not take back.
        Each one occupies a target this pass needs, so it is named in the
        refusal rather than surfacing as an unexplained "something is already
        there" about a file the plugin itself put down.
        """
        stray = _describe_leftovers(leftovers)
        done: list[tuple[str, str]] = []
        for source, target in pairs:
            failure = self._rename_one(source, target)
            if failure is None:
                done.append((source, target))
                continue
            stuck = self._roll_back(done)
            if not stuck:
                return {
                    "moved": [],
                    "stranded": [],
                    "unmoved": [pair_source for pair_source, _ in pairs],
                    "error": failure + stray,
                }
            return {
                "moved": [pair_target for _pair_source, pair_target in stuck],
                "stranded": [],
                "unmoved": [
                    pair_source
                    for pair_source, pair_target in pairs
                    if pair_target not in {stuck_target for _stuck_source, stuck_target in stuck}
                ],
                "error": f"{failure}; these are now at their new names and could not be put back: "
                + ", ".join(os.path.basename(pair_target) for _pair_source, pair_target in stuck)
                + stray,
            }
        return {"moved": [target for _source, target in pairs], "stranded": [], "unmoved": [], "error": ""}

    def _rename_one(self, source: str, target: str) -> str | None:
        """Rename one pair, refusing an occupied target. ``None`` on success."""
        if self.exists(target):
            return f"something is already at {os.path.basename(target)}"
        try:
            os.rename(source, target)
        except OSError as e:
            return f"could not move {os.path.basename(source)}: {e}"
        return None

    @staticmethod
    def _roll_back(done: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Move every completed pair back; return the ones that would not go back."""
        stuck: list[tuple[str, str]] = []
        for source, target in reversed(done):
            try:
                os.rename(target, source)
            except OSError:
                stuck.append((source, target))
        return stuck


def _describe_failure(source: str, error: OSError, leftovers: list[str]) -> str:
    """State a staging failure, and any link the undo could not take back with it."""
    return f"could not prepare {os.path.basename(source)}: {error}" + _describe_leftovers(leftovers)


def _describe_leftovers(leftovers: list[str] | None) -> str:
    """Name the links a staging attempt made and could not take back, or ``""``."""
    if not leftovers:
        return ""
    return "; these copies were left behind: " + ", ".join(os.path.basename(path) for path in leftovers)
