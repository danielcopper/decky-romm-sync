"""CandidateSearch — is this game already on disk, and what can be said about it.

Owns both halves of the question and the order the answers come in. The
game-detail page asks the cheap half (a name, a boolean); the Download click
asks the whole of it and turns the answer into the refusal the dialog renders.

The two halves read the same folder from different knowledge, deliberately: the
page holds a ``roms`` row and must stay network-free, while the click path holds
the server payload and the path the download derived. They have diverged four
times — shape, platform directory, matched name, and the directory listing
itself — so nothing here rests on them agreeing. What the button promises is
kept by the last answer in the chain instead: if the page found a copy and this
search cannot say anything specific about the folder, the download stops and
says so (ADR-0028).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.rom_adoption import is_archive_name, server_manifest
from domain.rom_candidates import (
    EntryT,
    LocalEntry,
    LocalName,
    candidates_refusal,
    matching_entries,
    normalize_rom_name,
    rank_candidates,
    shape_conflict_refusal,
    unreadable_refusal,
    vanished_candidate_refusal,
)
from domain.rom_files import is_multi_file_download, resolve_local_file_name
from lib.path_safety import safe_join

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        DebugLogger,
        DownloadFileStore,
        RetroDeckPaths,
        SystemKnownFn,
        SystemResolver,
        SystemSupportedExtensionsFn,
        UnitOfWorkFactory,
    )


def _whole_file_crc32(rom_detail: dict[str, Any]) -> str:
    """RomM's CRC32 for this ROM's one file, or ``""`` when it does not have exactly one.

    A ROM the server holds as several files publishes no single number that could
    describe one entry on disk, so the candidate search ranks those on size or
    name instead of on a checksum it would have to invent.
    """
    manifest = server_manifest(rom_detail)
    return manifest[0].crc32 if len(manifest) == 1 else ""


@dataclass(frozen=True)
class _Matches:
    """Every namesake in the folder, split by what can be said about it.

    The three are disjoint and together are every entry the name filter kept:
    one this search read and can offer, one it could not read at all, and one it
    read and rejected on shape.
    """

    offerable: tuple[LocalEntry, ...]
    unreadable: tuple[LocalEntry, ...]
    wrong_shape: tuple[LocalEntry, ...]


@dataclass(frozen=True)
class CandidateSearchConfig:
    """Frozen wiring bundle handed to ``CandidateSearch.__init__``.

    ``system_known`` and ``system_extensions`` are two questions to the same
    source, ``es_systems.xml``: whether the directory about to be searched is a
    system at all, and what that system accepts. Both answer default-safe when
    the file cannot be read, and the search must not turn either silence into a
    refusal.
    """

    download_file_store: DownloadFileStore
    resolve_system: SystemResolver
    system_extensions: SystemSupportedExtensionsFn
    system_known: SystemKnownFn
    retrodeck_paths: RetroDeckPaths
    uow_factory: UnitOfWorkFactory
    logger: logging.Logger
    log_debug: DebugLogger


class CandidateSearch:
    """Finds what could be a ROM in its platform folder, and says what it found."""

    def __init__(self, *, config: CandidateSearchConfig) -> None:
        self._download_file_store = config.download_file_store
        self._resolve_system = config.resolve_system
        self._system_extensions = config.system_extensions
        self._system_known = config.system_known
        self._retrodeck_paths = config.retrodeck_paths
        self._uow_factory = config.uow_factory
        self._logger = config.logger
        self._log_debug = config.log_debug

    # ── The Download click's half ───────────────────────────────────

    def refusal(
        self, rom_detail: dict[str, Any], checked_path: str, *, page_saw_candidate: bool
    ) -> dict[str, Any] | None:
        """Everything this search can refuse a download for, most specific first.

        ``None`` means proceed, which is every ordinary download. The order is
        the point — a user meets the specific explanation whenever one exists,
        and the generic one only when nothing better is known:

        1. ``adoption_candidates`` — entries of the shape the server serves that
           this search could read. The dialog can offer these.
        2. ``unreadable_entry`` — a namesake whose ``stat`` did not answer. It
           comes before the shape conflict because it may be the very copy the
           user meant (a link into a mount that is away), and nothing about it
           has been ruled out — where a wrong-shape entry is something that was
           read and found unusable.
        3. ``shape_conflict`` — a readable namesake in the other shape, which
           nothing can adopt.
        4. ``candidate_vanished`` — the backstop: the page found a copy, and
           none of the above applies.
        """
        platform_dir = os.path.dirname(checked_path)
        system = self._resolve_system(rom_detail.get("platform_slug", ""), rom_detail.get("platform_fs_slug"))
        incoming_name = os.path.basename(checked_path)
        incoming_size = rom_detail.get("fs_size_bytes", 0)
        wanted = self._wanted_names(rom_detail, incoming_name)
        matches = self._matches(
            self._local_entries(platform_dir),
            system=system,
            wanted=wanted,
            covered=self._installed_paths() | {checked_path},
            served_dir=is_multi_file_download(rom_detail),
        )
        self._log_debug(
            f"adopt search: dir={platform_dir} names={sorted(wanted)} "
            f"candidates={len(matches.offerable)} unreadable={len(matches.unreadable)} "
            f"wrong_shape={len(matches.wrong_shape)} page_saw_candidate={page_saw_candidate}"
        )
        rom_id = rom_detail.get("id")
        if matches.offerable:
            candidates, truncated = self._rank(matches.offerable, rom_detail)
            self._logger.info(f"Found {len(candidates)} adoption candidate(s) for rom {rom_id}")
            return candidates_refusal(
                candidates, truncated=truncated, incoming_name=incoming_name, incoming_size=incoming_size
            )
        if matches.unreadable:
            self._logger.info(f"Refusing rom {rom_id}: {len(matches.unreadable)} same-named entr(ies) unreadable")
            return unreadable_refusal(
                matches.unreadable,
                removable_paths=self._removable(matches.unreadable),
                incoming_name=incoming_name,
                incoming_size=incoming_size,
            )
        if matches.wrong_shape:
            self._logger.info(
                f"Refusing rom {rom_id}: {len(matches.wrong_shape)} same-named entr(ies) of the wrong shape"
            )
            return shape_conflict_refusal(
                matches.wrong_shape,
                served_dir=is_multi_file_download(rom_detail),
                incoming_name=incoming_name,
                incoming_size=incoming_size,
            )
        if page_saw_candidate:
            self._logger.info(f"Refusing rom {rom_id}: the page found a copy this search cannot account for")
            return vanished_candidate_refusal(incoming_name=incoming_name, incoming_size=incoming_size)
        return None

    def _wanted_names(self, rom_detail: dict[str, Any], incoming_name: str) -> frozenset[str]:
        """Every normalized name this ROM may be on disk under.

        The path the download derived is one of them; ``fs_name`` is the other,
        and for a ROM RomM serves as a folder around a differently-named file
        they are different strings. A user's own copy is named after the game and
        never after the inner file, so searching under the derived name alone
        finds nothing on exactly those platforms.
        """
        local_name, _missing = resolve_local_file_name(rom_detail)
        return frozenset(
            normalize_rom_name(name) for name in (incoming_name, local_name, rom_detail.get("fs_name", ""))
        )

    # ── The game page's half ────────────────────────────────────────

    def name_matches(self, platform_slug: str, fs_name: str) -> tuple[LocalName, ...]:
        """Entries in *platform_slug*'s folder whose normalized name is *fs_name*'s, either shape.

        The system is resolved from the slug alone — a ``roms`` row carries no
        ``platform_fs_slug``, which the resolver consults only for a slug that
        misses its platform map. The directory that comes out is then the RomM
        slug taken verbatim, which is why :meth:`_searchable_dir` refuses one
        that is not an ES-DE system: a namesake in such a directory is content
        no emulator will ever look at.
        """
        roms_path = self._retrodeck_paths.roms_path()
        if not roms_path or not fs_name or not platform_slug:
            return ()
        system = self._resolve_system(platform_slug)
        platform_dir = safe_join(roms_path, system)
        wanted = frozenset({normalize_rom_name(fs_name)})
        covered = self._installed_paths() | {os.path.join(platform_dir, fs_name)}
        entries = self._local_names(platform_dir)
        found = self._named(entries, system=system, wanted=wanted, covered=covered)
        self._log_debug(
            f"adopt probe: dir={platform_dir} name={normalize_rom_name(fs_name)} "
            f"entries={len(entries)} found={len(found)}"
        )
        return found

    # ── Shared ──────────────────────────────────────────────────────

    def _searchable_dir(self, system: str) -> bool:
        """Whether *system*'s directory is a place a game for it can live.

        ``es_systems.xml`` is the same source the accept-list comes from, so this
        cannot disagree with the extension filter. A source that could not answer
        is not a denial — the search proceeds exactly as it did before, and the
        accept-list's own empty answer keeps its default-safe reading.
        """
        return self._system_known(system) is not False

    def _named(
        self,
        entries: tuple[EntryT, ...],
        *,
        system: str,
        wanted: frozenset[str],
        covered: frozenset[str],
    ) -> tuple[EntryT, ...]:
        """Every name-matching entry in *entries*, of either shape.

        Both shapes come back from one pass because the caller decides what each
        one means: for the page any namesake is enough, and for the click path
        the shape is what separates a candidate from a conflict.
        """
        if not entries or not self._searchable_dir(system):
            return ()
        extensions = self._system_extensions(system)
        files = matching_entries(
            entries, wanted_names=wanted, want_dir=False, accepted_extensions=extensions, covered_paths=covered
        )
        dirs = matching_entries(
            entries, wanted_names=wanted, want_dir=True, accepted_extensions=extensions, covered_paths=covered
        )
        return files + dirs

    def _matches(
        self,
        entries: tuple[LocalEntry, ...],
        *,
        system: str,
        wanted: frozenset[str],
        covered: frozenset[str],
        served_dir: bool,
    ) -> _Matches:
        """Split every namesake into the three things the chain answers with."""
        found = self._named(entries, system=system, wanted=wanted, covered=covered)
        return _Matches(
            offerable=tuple(entry for entry in found if entry.readable and entry.is_dir == served_dir),
            unreadable=tuple(entry for entry in found if not entry.readable),
            wrong_shape=tuple(entry for entry in found if entry.readable and entry.is_dir != served_dir),
        )

    def _removable(self, entries: tuple[LocalEntry, ...]) -> frozenset[str]:
        """The subset of *entries* whose removal would destroy nothing."""
        return frozenset(entry.path for entry in entries if self._download_file_store.is_broken_symlink(entry.path))

    def _rank(self, matches: tuple[LocalEntry, ...], rom_detail: dict[str, Any]):
        """Order what matched by the evidence each entry supports without a byte being read."""
        return rank_candidates(
            matches,
            server_size=rom_detail.get("fs_size_bytes", 0),
            server_crc32=_whole_file_crc32(rom_detail),
            member_crc32s={match.path: self._member_crc32s(match) for match in matches},
        )

    def _local_entries(self, platform_dir: str) -> tuple[LocalEntry, ...]:
        """Read the platform folder's top level, sizes and mtimes included, for ranking."""
        return tuple(
            LocalEntry(
                name=entry["name"],
                path=entry["path"],
                is_dir=entry["is_dir"],
                size_bytes=entry["size_bytes"],
                modified_at=entry["modified_at"],
                readable=entry["readable"],
            )
            for entry in self._download_file_store.list_top_level_entries(platform_dir)
        )

    def _local_names(self, platform_dir: str) -> tuple[LocalName, ...]:
        """Read the platform folder's top level as names and shapes alone.

        The page's listing: no ``stat`` for size or mtime, because a name match
        reads neither. The type says so too — these cannot reach :meth:`_rank`,
        which is the half of the search the page skips.
        """
        return tuple(
            LocalName(name=entry["name"], path=entry["path"], is_dir=entry["is_dir"])
            for entry in self._download_file_store.list_top_level_names(platform_dir)
        )

    def _member_crc32s(self, entry: LocalEntry) -> tuple[str, ...]:
        """The CRC32 of every member inside *entry*, from the central directory alone.

        Empty for anything that is not an archive by name and for a container this
        store cannot open — an absence of evidence, which leaves the entry ranked
        on what else it has.
        """
        if entry.is_dir or not is_archive_name(entry.name):
            return ()
        members = self._download_file_store.list_archive_members(entry.path)
        return () if members is None else tuple(member["crc32"] for member in members)

    def _installed_paths(self) -> frozenset[str]:
        """Every path a ``rom_installs`` row already accounts for.

        A ROM the plugin installed is another game's content and the row is the
        plugin's claim on it, so the search subtracts it rather than offering one
        game's files as a candidate for another's.
        """
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
        return frozenset(
            path for install in installs for path in (install.file_path, install.rom_dir) if path is not None
        )
