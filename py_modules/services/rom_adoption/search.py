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
    DIR,
    FILE,
    EntryT,
    LocalEntry,
    LocalName,
    candidates_refusal,
    matching_entries,
    normalize_rom_name,
    rank_candidates,
    unusable_namesake_refusal,
    vanished_candidate_refusal,
)
from domain.rom_files import is_multi_file_download, resolve_local_file_name
from lib.path_safety import PathTraversalError, safe_join

if TYPE_CHECKING:
    import logging
    from collections.abc import Sequence

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
    """Every namesake in the folder, split by whether it could become this install.

    The two are disjoint and together are every entry the name filter kept. A
    namesake is offerable only if it is the shape the server serves **and** a
    kind an install row may point at; everything else is unusable — the other
    shape, or a symlink, which no install row may point at whatever it resolves
    to.
    """

    offerable: tuple[LocalEntry, ...]
    unusable: tuple[LocalEntry, ...]


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

        ``None`` means proceed, which is every ordinary download. Three answers,
        and the order is the point — a user meets the specific explanation
        whenever one exists, and the generic one only when nothing better is
        known:

        1. ``adoption_candidates`` — entries the dialog can offer: the shape the
           server serves, and a kind an install row may point at.
        2. ``unusable_namesake`` — this game's name on something that cannot
           become the install: the other shape, or a symlink. Worth saying,
           because a download lands beside it and leaves the user two.
        3. ``candidate_vanished`` — the backstop: the page found a copy, and
           neither of the above applies.
        """
        platform_dir = os.path.dirname(checked_path)
        system = self._resolve_system(rom_detail.get("platform_slug", ""), rom_detail.get("platform_fs_slug"))
        incoming_name = os.path.basename(checked_path)
        incoming_size = rom_detail.get("fs_size_bytes", 0)
        wanted = self._wanted_names(rom_detail, incoming_name)
        served_dir = is_multi_file_download(rom_detail)
        matches = self._matches(
            self._local_entries(platform_dir),
            system=system,
            wanted=wanted,
            covered=self._installed_paths() | {checked_path},
            served_dir=served_dir,
        )
        self._log_debug(
            f"adopt search: dir={platform_dir} names={sorted(wanted)} "
            f"candidates={len(matches.offerable)} unusable={len(matches.unusable)} "
            f"page_saw_candidate={page_saw_candidate}"
        )
        rom_id = rom_detail.get("id")
        if matches.offerable:
            candidates, truncated = self._rank(matches.offerable, rom_detail)
            self._logger.info(f"Found {len(candidates)} adoption candidate(s) for rom {rom_id}")
            return candidates_refusal(
                candidates, truncated=truncated, incoming_name=incoming_name, incoming_size=incoming_size
            )
        if matches.unusable:
            self._logger.info(f"Refusing rom {rom_id}: {len(matches.unusable)} same-named entr(ies) cannot be adopted")
            return unusable_namesake_refusal(
                matches.unusable,
                served_dir=served_dir,
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
        """Entries in *platform_slug*'s folder whose normalized name is *fs_name*'s.

        Every kind comes back, because the page's question is "is this game here
        at all" — a symlink the click path will refuse to adopt is still content
        the user has, and a button that ignored it would send them to a dialog
        they were told nothing about.

        The system is resolved from the slug alone — a ``roms`` row carries no
        ``platform_fs_slug``, which the resolver consults only for a slug that
        misses its platform map. The directory that comes out is then the RomM
        slug taken verbatim, which is why :meth:`_searchable_dir` refuses one
        that is not an ES-DE system: a namesake in such a directory is content
        no emulator will ever look at.
        """
        system = self._resolve_system(platform_slug)
        platform_dir = self._platform_dir(system)
        wanted_name = normalize_rom_name(fs_name)
        if platform_dir is None or not wanted_name:
            self._log_probe(platform_slug, platform_dir, wanted_name, entries=(), found=())
            return ()
        covered = self._installed_paths() | {os.path.join(platform_dir, fs_name)}
        entries = self._local_names(platform_dir)
        found = self._named(entries, system=system, wanted=frozenset({wanted_name}), covered=covered)
        self._log_probe(platform_slug, platform_dir, wanted_name, entries=entries, found=found)
        return found

    def _log_probe(
        self,
        platform_slug: str,
        platform_dir: str | None,
        wanted_name: str,
        *,
        entries: Sequence[LocalName],
        found: Sequence[LocalName],
    ) -> None:
        """The probe's one log line, the same keys whichever exit reached it.

        This log exists to reconstruct a divergence between the page and the
        click search after the fact, and a line whose shape changes per exit is
        exactly what makes that hard to read. So every exit states all five, and
        the ones that gave up early say so in the value rather than by omitting
        the key: an unresolvable directory is ``unresolved``, a name with nothing
        left to match on is ``<empty>``.
        """
        self._log_debug(
            f"adopt probe: slug={platform_slug} dir={platform_dir or 'unresolved'} "
            f"name={wanted_name or '<empty>'} entries={len(entries)} found={len(found)}"
        )

    def _platform_dir(self, system: str) -> str | None:
        """The folder *system*'s games live in, derived as the download derives it.

        ``safe_join`` is the download's own derivation (``services/downloads.py``
        builds every target path with it), and it resolves symlinks. Both sides
        must use it or neither may: with the ROMs root behind a link — a library
        on removable storage — one side would describe a file under a path the
        other never produces, and the install rows the search subtracts are
        recorded under the download's spelling. The page would then report a copy
        the click search cannot find, and the backstop would fire on the
        plugin's own installs.

        ``None`` for anything that cannot be derived, including the traversal
        ``safe_join`` refuses; the caller's answer is "no candidate", which is
        what an unresolvable directory honestly supports.
        """
        roms_path = self._retrodeck_paths.roms_path()
        if not roms_path or not system:
            return None
        try:
            return safe_join(roms_path, system)
        except PathTraversalError:
            return None

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
        """Every name-matching entry in *entries*, of every kind.

        One pass, because the caller decides what each kind means: for the page
        any namesake is enough, and for the click path the kind is what separates
        a candidate from something to warn about.
        """
        if not entries or not self._searchable_dir(system):
            return ()
        return matching_entries(
            entries,
            wanted_names=wanted,
            accepted_extensions=self._system_extensions(system),
            covered_paths=covered,
        )

    def _matches(
        self,
        entries: tuple[LocalEntry, ...],
        *,
        system: str,
        wanted: frozenset[str],
        covered: frozenset[str],
        served_dir: bool,
    ) -> _Matches:
        """Split every namesake into the two things the chain answers with.

        The served kind is a file or a directory and never a link, so an entry
        matching it is by construction one an install row may point at — no
        second test needed, and no way for a symlink to reach the offerable side.
        """
        found = self._named(entries, system=system, wanted=wanted, covered=covered)
        served_kind = DIR if served_dir else FILE
        offerable = tuple(entry for entry in found if entry.kind == served_kind)
        return _Matches(offerable=offerable, unusable=tuple(entry for entry in found if entry.kind != served_kind))

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
                kind=entry["kind"],
                size_bytes=entry["size_bytes"],
                modified_at=entry["modified_at"],
            )
            for entry in self._download_file_store.list_top_level_entries(platform_dir)
        )

    def _local_names(self, platform_dir: str) -> tuple[LocalName, ...]:
        """Read the platform folder's top level as names and kinds alone.

        The page's listing: no ``stat`` for size or mtime, because a name match
        reads neither. The type says so too — these cannot reach :meth:`_rank`,
        which is the half of the search the page skips.
        """
        return tuple(
            LocalName(name=entry["name"], path=entry["path"], kind=entry["kind"])
            for entry in self._download_file_store.list_top_level_names(platform_dir)
        )

    def _member_crc32s(self, entry: LocalEntry) -> tuple[str, ...]:
        """The CRC32 of every member inside *entry*, from the central directory alone.

        Empty for anything that is not an archive by name and for a container this
        store cannot open — an absence of evidence, which leaves the entry ranked
        on what else it has.
        """
        if entry.kind == DIR or not is_archive_name(entry.name):
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
