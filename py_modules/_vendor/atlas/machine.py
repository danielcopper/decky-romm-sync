"""The machine seam — the one injected protocol every machine access flows through.

atlas is a resolver: it answers questions about the running machine by reading
the running machine. All of that reading goes through a :class:`Machine`, which
abstracts the machine itself — files, directories, symlinks, and the answers
only a core binary can give. In production the machine is the real one
(:class:`RealMachine`); in tests and conformance vectors it is a fixture
(:class:`FixtureMachine`): files, directories, symlinks, and core answers as
plain data describing a whole machine, including broken links, unreadable
files, and unloadable cores. One code path, two data sources, so everything
atlas concludes is provable from data — the failure states included.

Every operation reports an explicit outcome instead of collapsing failure
modes: ``read_text`` distinguishes *missing* from *unreadable* from
*invalid text*, and ``path_kind`` distinguishes a file from a directory from
an inaccessible path. The resolver needs those distinctions because the
emulators make them — RetroArch applies a configured directory only when
``path_is_directory()`` succeeds — and because health reporting must never
present a present-but-broken installation as absent or healthy.

``readlink`` exists because RetroDECK's standalone save architecture is symlinks
(``dir_prep``): the emulator-side path and the real path are two truthful
answers to different questions, and a dead link is a real state the resolver
must be able to see. ``query_core`` exists because ``library_name`` — the value
that names sort-by-core directories and the override directory — lives only in
the core binary; loading the core and asking it is the same read RetroArch
performs. A live read, never a shipped table.

``file_size`` and ``file_digest`` exist because firmware identity is checked by
content, not by name: a file present under the right name may still be the
wrong dump. ``file_size`` is the free pre-filter (one ``stat``) that settles
most mismatches before any bytes are hashed; ``file_digest`` is the paid
answer. Both return ``None`` for *cannot tell* — never a sentinel that a caller
could mistake for a real value.

Path resolution (normative, for ports): a path is walked component by component
from ``/``, the way ``path_resolution(7)`` describes and the kernel was observed
to behave. ``.`` and repeated separators are transparent; ``..`` is applied to
where the walk *landed*, so ``link/..`` leaves the link's target directory, not
the directory the link sits in; a symlink component is replaced by its target
and the walk continues. Every component the walk steps *through* must be a
directory, which is what makes the failure spellings answer as they do: a
trailing ``/`` on a regular file is ``ENOTDIR``, reported as *missing*, while
the same spelling on a directory is transparent. Lexical normalization
(``os.path.normpath``) is not this: it eats the component in front of a ``..``
even when that component is a symlink, and the kernel does the opposite.

Glob semantics (normative, for ports): patterns support ``*``, ``?`` and
``[seq]`` within one path segment; a wildcard never crosses a ``/``; a wildcard
segment never matches a name starting with ``.`` (only a segment that itself
starts with ``.`` does); a wildcard segment that is not the last one matches
directories only, following symlinks to decide; a pattern ending in ``/``
matches directories only and keeps that ``/``; a relative pattern matches
nothing, because the working directory is not a fact about the machine; matches
are returned sorted, spelled the way the pattern reached them (a file matched
through a symlinked directory keeps the link-side path).

``glob`` reports **how much of the walk it could read**, because "the directory
is empty" and "the directory could not be listed" are the same empty list
otherwise — and the second is the shape of a save directory on a card that
dropped off the bus. A pattern can need several directories, so the answer is
per-directory rather than all-or-nothing: ``matches`` carries what *was* found
and ``unreadable`` names every place the walk could not look. Not every empty
answer is a failure: a name that is not there, or a path component that is not
a directory, is a truthful negative and keeps the answer ``complete`` — only a
read that *failed* (permissions, a symlink loop, I/O) makes it ``incomplete``.
This is the one operation whose payload is partial rather than absent, and it
is why the two are separate fields instead of a status alone.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat as _stat
import subprocess
import sys
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Iterable, Literal, Mapping, Protocol

from . import ps2_bios, squashfs

_CORE_PROBE_TIMEOUT_SECONDS = 15
SYMLINK_HOPS = 40
"""How many symlink hops a path may take before it counts as unresolvable.

The number is the kernel's, observed rather than assumed: chains of 38, 39 and
40 links built in a scratch directory all ``stat`` and ``open`` fine, and 41
answers ``ELOOP`` (Linux 6.16, ``SYMLOOP_MAX``). So *this many* hops resolve and
the next one does not, and every resolver in atlas uses this one number — the
two machines and :func:`atlas.firmware.resolve_links` alike. A resolver that
disagreed inside some window would make every vector built on it prove nothing,
and the window would be exactly one hop wide, which is where nobody looks.
"""

# Digest algorithms the seam answers for. Closed on purpose: these are the two
# libretro-database's System.dat carries, and a port must implement exactly
# them (an open algorithm parameter would make conformance unprovable).
DIGEST_MD5 = "md5"
DIGEST_SHA1 = "sha1"
DIGEST_ALGORITHMS = (DIGEST_MD5, DIGEST_SHA1)

_DIGEST_CHUNK_BYTES = 1 << 20

ReadStatus = Literal["ok", "missing", "unreadable", "invalid-text"]
PathKind = Literal["file", "directory", "missing", "inaccessible"]
GlobStatus = Literal["complete", "incomplete"]

# An AppImage read has three more ways to fail than a plain file read, and
# each is a different claim a caller acts on differently: "not-appimage" is a
# file that exists and is not an AppImage-with-squashfs (replaced, truncated,
# some other executable); "entry-missing" is a healthy image without the asked
# entry (an upstream restructuring); "capability-missing" is this runtime
# lacking the image's codec (zstd needs Python 3.14's compression.zstd or the
# backports.zstd package) — the file is fine, the runtime is what cannot open
# it, and reporting it as any file state would blame the machine for the
# process.
AppImageReadStatus = Literal[
    "ok", "missing", "unreadable", "invalid-text", "not-appimage", "entry-missing", "capability-missing"
]

READ_OK: ReadStatus = "ok"
READ_MISSING: ReadStatus = "missing"
READ_UNREADABLE: ReadStatus = "unreadable"
READ_INVALID_TEXT: ReadStatus = "invalid-text"

APPIMAGE_NOT_APPIMAGE: AppImageReadStatus = "not-appimage"
APPIMAGE_ENTRY_MISSING: AppImageReadStatus = "entry-missing"
APPIMAGE_CAPABILITY_MISSING: AppImageReadStatus = "capability-missing"

# A PS2 BIOS header read has one way to fail beyond a plain file read:
# "not-a-bios" is a file that was opened and read and fails the core's own
# test (atlas.ps2_bios) — a finding about the bytes, never a read failure,
# which is why it is its own word rather than a missing header.
Ps2BiosHeaderStatus = Literal["ok", "missing", "unreadable", "not-a-bios"]
# The plain read's three words again, typed as this read's so a result can be
# built from them; the fourth is this read's own.
PS2_BIOS_OK: Ps2BiosHeaderStatus = "ok"
PS2_BIOS_MISSING: Ps2BiosHeaderStatus = "missing"
PS2_BIOS_UNREADABLE: Ps2BiosHeaderStatus = "unreadable"
PS2_BIOS_NOT_A_BIOS: Ps2BiosHeaderStatus = "not-a-bios"

KIND_FILE: PathKind = "file"
KIND_DIRECTORY: PathKind = "directory"
KIND_MISSING: PathKind = "missing"
KIND_INACCESSIBLE: PathKind = "inaccessible"

GLOB_COMPLETE: GlobStatus = "complete"
GLOB_INCOMPLETE: GlobStatus = "incomplete"


@dataclass(frozen=True, slots=True)
class ReadResult:
    """One text read's explicit outcome — ``text`` is set exactly when ``status`` is ok.

    ``missing`` means the path (or a parent component) does not exist;
    ``unreadable`` means it exists but cannot be read (permissions, a
    directory, I/O error); ``invalid-text`` means bytes exist but are not
    valid UTF-8 text. The distinctions are health signals, never collapsed.
    """

    status: ReadStatus
    text: str | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.status == READ_OK):
            raise ValueError(f"ReadResult: text must be set exactly when status is 'ok' (got {self.status!r})")


@dataclass(frozen=True, slots=True)
class AppImageReadResult:
    """One AppImage-entry read's explicit outcome — the plain read's shape, wider.

    The three extra statuses are documented on :data:`AppImageReadStatus`;
    ``missing`` / ``unreadable`` / ``invalid-text`` mean what they mean on
    :class:`ReadResult`, with ``invalid-text`` judging the *entry's* bytes.
    """

    status: AppImageReadStatus
    text: str | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.status == READ_OK):
            raise ValueError(
                f"AppImageReadResult: text must be set exactly when status is 'ok' (got {self.status!r})"
            )


@dataclass(frozen=True, slots=True)
class Ps2BiosHeaderResult:
    """One ROMDIR header read's explicit outcome — ``header`` is set exactly when ``status`` is ok.

    ``missing`` / ``unreadable`` mean what they mean on :class:`ReadResult`;
    ``not-a-bios`` is documented on :data:`Ps2BiosHeaderStatus`. The header is
    :class:`atlas.ps2_bios.Ps2BiosHeader`, the fields the core extracts.
    """

    status: Ps2BiosHeaderStatus
    header: ps2_bios.Ps2BiosHeader | None = None

    def __post_init__(self) -> None:
        if (self.header is None) == (self.status == PS2_BIOS_OK):
            raise ValueError(
                f"Ps2BiosHeaderResult: header must be set exactly when status is 'ok' (got {self.status!r})"
            )


@dataclass(frozen=True, slots=True)
class GlobResult:
    """One glob's explicit outcome — what matched, and what could not be read.

    ``unreadable`` is non-empty exactly when ``status`` is ``incomplete``, the
    same shape of invariant :class:`ReadResult` carries. The difference is that
    a failed glob still has an answer worth having: a pattern spanning several
    directories can read some and not others, so ``matches`` is what was found
    *and* ``unreadable`` names where the walk stopped short. A caller that only
    wants the files can read ``matches`` and be no worse off than with a bare
    list; a caller that would otherwise say "there is nothing there" has to look
    at the status first, which is the whole point of the type.

    ``unreadable`` holds directories that could not be listed and entries that
    could not be looked at — the places, not the reasons. Whether it was
    permissions, a symlink loop, or a card that stopped answering is not a
    distinction any caller here acts on, and the path is what a message needs.
    """

    status: GlobStatus
    matches: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if bool(self.unreadable) != (self.status == GLOB_INCOMPLETE):
            raise ValueError(
                "GlobResult: unreadable must be non-empty exactly when status is 'incomplete' "
                f"(got {self.status!r} with {len(self.unreadable)} unreadable paths)"
            )


@dataclass(frozen=True, slots=True)
class CoreOption:
    """One option definition a core registers: its default and legal values.

    Captured from the registration call the core makes during
    ``retro_set_environment`` — the same declaration RetroArch's option
    manager validates persisted values against. A live read of the binary,
    never shipped data.
    """

    key: str
    default: str | None
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoreInfo:
    """What a libretro core reports about itself.

    ``library_name`` (via ``retro_get_system_info``) is the value RetroArch
    uses for sort-by-core directories and override directories — the display
    name, not the ``.so`` basename: the two disagree for 183 of the 210 loadable
    cores RetroDECK ships (reference machine, recounted 2026-08-05). ``options``
    is the set of option definitions the core registered during
    ``retro_set_environment`` — the observable fact that identifies a
    core *generation* better than any version string. ``None`` means *not
    captured* (the probe saw no registration — some cores register later, in
    ``retro_init``): unknown, never "registers nothing".

    ``block_extract`` is the same struct's archive statement
    (``retro_system_info.block_extract``): true means RetroArch hands the
    core an archive raw instead of picking a matching file out of it
    (task_content.c:742, :1735 @ a79435a). ``None`` is a fixture or record
    that never captured it — unknown, never "false".
    """

    library_name: str
    library_version: str | None
    valid_extensions: str | None
    options: Mapping[str, CoreOption] | None = None
    block_extract: bool | None = None

    def __post_init__(self) -> None:
        if self.options is not None:
            object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


_GLOB_MAGIC = frozenset("*?[")


def _has_magic(text: str) -> bool:
    return bool(_GLOB_MAGIC & set(text))


@dataclass(slots=True)
class _GlobWalk:
    """The glob algorithm itself, over three primitives each machine supplies.

    One algorithm, two data sources — the same reason the seam exists at all.
    Pattern decomposition and result *spelling* are subtle enough that two
    independent implementations drifted apart on doubled separators alone, and
    a fixture that spells a match differently from the machine makes the vector
    built on it prove nothing. So the machines supply only what they alone can
    answer, and share everything derived from it.

    The decomposition is CPython's (``glob.py``: split off the last segment,
    glob the rest, then read inside each result), because that is what decides
    the spelling: ``os.path.split`` strips the separator run before the last
    segment while runs deeper inside survive verbatim, so ``a//b//*`` answers
    ``a//b/x``. A left-to-right walk cannot reproduce that, and the real
    machine's own glob is the thing being modelled.

    Each primitive answers ``None`` for *could not tell*, which is what the
    stdlib throws away (``glob.py:173`` returns on any ``OSError``) and what
    this exists to state. A truthful negative — the name is not there, the
    component is not a directory — is ``False``/``[]``, never ``None``.
    """

    list_dir: Callable[[str], list[str] | None]
    is_dir: Callable[[str], bool | None]
    lexists: Callable[[str], bool | None]
    unreadable: set[str] = field(default_factory=set)

    def run(self, pattern: str) -> GlobResult:
        if not pattern.startswith("/"):
            # A relative pattern resolves against the process's working
            # directory, which is not a fact about the machine being read.
            return GlobResult(GLOB_COMPLETE)
        matches = self._glob(pattern, dironly=False)
        if not self.unreadable:
            return GlobResult(GLOB_COMPLETE, tuple(sorted(matches)))
        return GlobResult(GLOB_INCOMPLETE, tuple(sorted(matches)), tuple(sorted(self.unreadable)))

    def _glob(self, pathname: str, *, dironly: bool) -> list[str]:
        """Every path *pathname* matches, spelled the way the pattern reached it."""
        dirname, basename = os.path.split(pathname)
        if not _has_magic(pathname):
            return [pathname] if self._literal_is_there(pathname, dirname, basename) else []
        if _has_magic(dirname):
            parents = self._glob(dirname, dironly=True)
        else:
            # No wildcard above: the directory is taken as spelled, and whether
            # it is there at all is answered by reading inside it.
            parents = [dirname]
        return [
            os.path.join(parent, name)
            for parent in parents
            for name in self._names_in(parent, basename, dironly=dironly)
        ]

    def _literal_is_there(self, pathname: str, dirname: str, basename: str) -> bool:
        """A pattern with no wildcard at all: it matches itself, if it is there.

        A trailing separator asks about the directory instead, and follows
        links to decide — ``link-to-dir/`` is a directory, ``link-to-file/`` is
        not, and a dead one is not either.
        """
        answer = self.lexists(pathname) if basename else self.is_dir(dirname)
        if answer is None:
            self.unreadable.add(pathname if basename else dirname)
            return False
        return answer

    def _names_in(self, dirname: str, basename: str, *, dironly: bool) -> list[str]:
        """The names inside *dirname* that *basename* selects."""
        if not _has_magic(basename):
            return self._literal_name_in(dirname, basename)
        names = self.list_dir(dirname)
        if names is None:
            self.unreadable.add(dirname)
            return []
        if not basename.startswith("."):
            # A wildcard never matches a leading dot; a segment that starts
            # with one is how a pattern asks for hidden names.
            names = [name for name in names if not name.startswith(".")]
        matched = fnmatch.filter(names, basename)
        return self._directories_among(dirname, matched) if dironly else matched

    def _literal_name_in(self, dirname: str, basename: str) -> list[str]:
        """A literal segment under a globbed parent — asked about, never listed.

        Listing is not the same read: a directory may be searchable and not
        readable, and then a name atlas already knows is answerable while the
        listing is not. Asking is also what the stdlib does, so the two agree
        wherever nothing fails.
        """
        target = os.path.join(dirname, basename) if basename else dirname
        # An empty basename is a pattern's trailing separator: it asks whether
        # the directory is one, not whether a name is there.
        answer = self.lexists(target) if basename else self.is_dir(target)
        if answer is None:
            self.unreadable.add(target)
            return []
        return [basename] if answer else []

    def _directories_among(self, dirname: str, names: list[str]) -> list[str]:
        """Only the names a wildcard may descend through — directories, links followed."""
        kept: list[str] = []
        for name in names:
            joined = os.path.join(dirname, name)
            answer = self.is_dir(joined)
            if answer is None:
                self.unreadable.add(joined)
            elif answer:
                kept.append(name)
        return kept


class Machine(Protocol):
    """Narrow machine port: read a file, glob, classify a path, follow links, ask a core.

    Every operation reports an explicit outcome; the caller decides what a
    failure means — the seam never guesses. ``glob`` follows the normative
    semantics in the module docstring and says how much of its walk it could
    read; a caller that would otherwise report "nothing is there" must look at
    that before believing an empty ``matches``. ``readlink`` returns the link target
    when the path itself is a symlink, else ``None``. ``query_core`` returns
    the core's self-reported info, or ``None`` when the core cannot be loaded —
    the caller treats that as *unknown*, never as a guess. ``file_size`` and
    ``file_digest`` answer for regular files only and return ``None`` whenever
    the answer cannot be determined (missing, unreadable, not a regular file,
    or an algorithm outside :data:`DIGEST_ALGORITHMS`).
    """

    def read_text(self, path: str) -> ReadResult: ...

    def read_appimage_text(self, path: str, inner_path: str) -> AppImageReadResult: ...

    def read_ps2_bios_header(self, path: str) -> Ps2BiosHeaderResult: ...

    def glob(self, pattern: str) -> GlobResult: ...

    def path_kind(self, path: str) -> PathKind: ...

    def readlink(self, path: str) -> str | None: ...

    def query_core(self, so_path: str) -> CoreInfo | None: ...

    def file_size(self, path: str) -> int | None: ...

    def file_digest(self, path: str, algorithm: str) -> str | None: ...


class RealMachine:
    """The production machine: the real filesystem plus a real core prober.

    ``query_core`` runs the probe in a subprocess (``atlas._core_probe``) so a
    crashing core costs one answer, not the host process, and memoizes per
    ``(path, mtime, size)`` — a cached live read, not shipped data: the cache
    invalidates the moment the ``.so`` changes. The child is pointed back at
    this package (:func:`_probe_environment`) and answers with whatever it
    printed before it stopped (:func:`_parse_probe_output`).
    """

    def __init__(self) -> None:
        self._core_cache: dict[tuple[str, int, int], CoreInfo | None] = {}

    def read_text(self, path: str) -> ReadResult:
        # Regular files only, checked BEFORE opening — for the same reason
        # file_digest checks: opening a FIFO with no writer blocks forever, and
        # these paths come out of config files. A ``.info`` that is a FIFO would
        # hang the whole firmware answer rather than degrade it.
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return ReadResult(READ_MISSING)
        except OSError:
            return ReadResult(READ_UNREADABLE)
        if not _stat.S_ISREG(st.st_mode):
            # A directory, a FIFO, a device: present, and not readable as text.
            return ReadResult(READ_UNREADABLE)
        try:
            with open(path, encoding="utf-8") as f:
                return ReadResult(READ_OK, f.read())
        except (FileNotFoundError, NotADirectoryError):
            return ReadResult(READ_MISSING)
        except UnicodeDecodeError:
            return ReadResult(READ_INVALID_TEXT)
        except OSError:
            # Permissions, I/O failure: present but unreadable.
            return ReadResult(READ_UNREADABLE)

    def read_appimage_text(self, path: str, inner_path: str) -> AppImageReadResult:
        """One entry out of an AppImage's embedded squashfs, decoded as UTF-8.

        The reader is :mod:`atlas.squashfs`; every failure it can name maps to
        its own status, because the caller's next step differs for each — a
        missing file is the AppImage being gone, ``capability-missing`` is
        this interpreter lacking the image's codec while the file is fine.
        """
        try:
            data = squashfs.read_appimage_entry(path, inner_path)
        except (FileNotFoundError, NotADirectoryError):
            return AppImageReadResult(READ_MISSING)
        except IsADirectoryError:
            return AppImageReadResult(READ_UNREADABLE)
        except squashfs.CodecUnavailable:
            return AppImageReadResult(APPIMAGE_CAPABILITY_MISSING)
        except squashfs.EntryNotFound:
            return AppImageReadResult(APPIMAGE_ENTRY_MISSING)
        except squashfs.SquashfsError:
            return AppImageReadResult(APPIMAGE_NOT_APPIMAGE)
        except OSError:
            return AppImageReadResult(READ_UNREADABLE)
        try:
            return AppImageReadResult(READ_OK, data.decode("utf-8"))
        except UnicodeDecodeError:
            return AppImageReadResult(READ_INVALID_TEXT)

    def read_ps2_bios_header(self, path: str) -> Ps2BiosHeaderResult:
        """The ROMDIR read LRPS2 makes over a folder candidate — a status, and the fields where it passes.

        Regular files only, checked before opening, for the reason
        ``file_digest`` checks: the folder route hands this whatever a listed
        directory holds. The reader is :mod:`atlas.ps2_bios`; its one refusal
        maps to ``not-a-bios``, and every read failure keeps the plain read's
        words, so a caller can tell a file that failed the test from a file
        that could not be given it.
        """
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return Ps2BiosHeaderResult(PS2_BIOS_MISSING)
        except OSError:
            return Ps2BiosHeaderResult(PS2_BIOS_UNREADABLE)
        if not _stat.S_ISREG(st.st_mode):
            return Ps2BiosHeaderResult(PS2_BIOS_UNREADABLE)
        try:
            with open(path, "rb") as f:
                header = ps2_bios.read_header(f)
        except (FileNotFoundError, NotADirectoryError):
            return Ps2BiosHeaderResult(PS2_BIOS_MISSING)
        except ps2_bios.NotAPs2Bios:
            return Ps2BiosHeaderResult(PS2_BIOS_NOT_A_BIOS)
        except OSError:
            return Ps2BiosHeaderResult(PS2_BIOS_UNREADABLE)
        return Ps2BiosHeaderResult(PS2_BIOS_OK, header)

    def glob(self, pattern: str) -> GlobResult:
        return _GlobWalk(self._list_dir, self._is_dir, self._lexists).run(pattern)

    @staticmethod
    def _list_dir(path: str) -> list[str] | None:
        """The names in one directory, or ``None`` when it could not be read.

        The split is the seam's usual one: a path that is not there and a
        component that is not a directory are truthful negatives, everything
        else is a read that failed. ``os.scandir`` was observed to raise
        ``PermissionError`` on a mode-000 directory and ``OSError(ELOOP)`` on a
        link cycle — the two states the stdlib's glob turns into an empty
        listing indistinguishable from an empty directory.
        """
        try:
            with os.scandir(path) as entries:
                return [entry.name for entry in entries]
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError:
            return None

    @staticmethod
    def _is_dir(path: str) -> bool | None:
        # Follows links, like the stdlib's per-entry ``is_dir()``: a link to a
        # directory may be descended through, a dead one may not, and a cycle
        # cannot be decided at all.
        try:
            return _stat.S_ISDIR(os.stat(path).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return None

    @staticmethod
    def _lexists(path: str) -> bool | None:
        # lstat, not stat: a dead symlink is a name a listing shows, so a glob
        # matches it (and the resolver has to see it — RetroDECK's dir_prep
        # leaves them behind).
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return None
        return True

    def path_kind(self, path: str) -> PathKind:
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return KIND_MISSING
        except OSError:
            return KIND_INACCESSIBLE
        return KIND_DIRECTORY if _stat.S_ISDIR(st.st_mode) else KIND_FILE

    def readlink(self, path: str) -> str | None:
        try:
            return os.readlink(path) if os.path.islink(path) else None
        except OSError:
            return None

    def file_size(self, path: str) -> int | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return st.st_size if _stat.S_ISREG(st.st_mode) else None

    def file_digest(self, path: str, algorithm: str) -> str | None:
        if algorithm not in DIGEST_ALGORITHMS:
            return None
        # Regular files only, checked BEFORE opening: reading a FIFO or a
        # character device blocks forever, and this runs inside a library entry
        # point that hashes whatever a config points at. A hang is not a
        # degraded answer, it is no answer at all.
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not _stat.S_ISREG(st.st_mode):
            return None
        digest = hashlib.new(algorithm)
        try:
            with open(path, "rb") as f:
                while chunk := f.read(_DIGEST_CHUNK_BYTES):
                    digest.update(chunk)
        except OSError:
            # Unreadable, or an I/O failure mid-read: the identity cannot be
            # stated. (A path that stopped being a regular file between the
            # stat and the open lands here too.)
            return None
        return digest.hexdigest()

    def query_core(self, so_path: str) -> CoreInfo | None:
        try:
            st = os.stat(so_path)
        except OSError:
            return None
        key = (so_path, st.st_mtime_ns, st.st_size)
        if key in self._core_cache:
            return self._core_cache[key]
        info = self._probe(so_path)
        # Only successes are memoized: a failure can be transient (missing
        # host library installed later) even while the .so is unchanged.
        if info is not None:
            self._core_cache[key] = info
        return info

    @staticmethod
    def _probe(so_path: str) -> CoreInfo | None:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "atlas._core_probe", so_path],
                capture_output=True,
                timeout=_CORE_PROBE_TIMEOUT_SECONDS,
                env=_probe_environment(),
            )
        except subprocess.TimeoutExpired as expired:
            # A core that hangs in the option-capture phase printed its base
            # answer before it hung; the exception carries what was captured.
            return _parse_probe_output(expired.stdout)
        except OSError:
            # The probe never ran — nothing was read, nothing can be said.
            return None
        return _parse_probe_output(proc.stdout)


def _parse_probe_output(stdout: bytes | None) -> CoreInfo | None:
    """Read the core's answer out of whatever the probe printed before it stopped.

    The probe prints one JSON object per line and later lines enrich earlier
    ones (two-phase design), so the last valid line wins. How the process
    *ended* is deliberately not consulted: the subprocess exists because cores
    crash — in ``retro_set_environment``, the option-capture phase — and by
    then the phase-1 answer carrying ``library_name`` has already been
    delivered. Discarding it on a non-zero exit would throw away a read that
    succeeded. A run that printed no usable line is ``None``: unknown, whether
    it exited cleanly or not.
    """
    data: dict[str, object] | None = None
    for line in (stdout or b"").decode("utf-8", "replace").splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            data = candidate
    if data is None:
        return None
    name = data.get("library_name")
    if not isinstance(name, str) or not name:
        return None
    version = data.get("library_version")
    extensions = data.get("valid_extensions")
    block_extract = data.get("block_extract")
    return CoreInfo(
        library_name=name,
        library_version=version if isinstance(version, str) else None,
        valid_extensions=extensions if isinstance(extensions, str) else None,
        options=_parse_core_options(data.get("options")),
        block_extract=block_extract if isinstance(block_extract, bool) else None,
    )


def _probe_environment() -> dict[str, str] | None:
    """The probe child's environment: this package's location ahead of ``PYTHONPATH``.

    The child imports ``atlas._core_probe`` by name, and under the vendoring
    model — this package copied into a host that puts it on ``sys.path`` at
    runtime — nothing on the child's default path leads back to it: every core
    would come back unknown, for a missing module rather than for anything
    about the core. Prepending the directory that holds this package keeps the
    child on the same atlas the parent runs, and leaves the rest of the
    environment, an inherited ``PYTHONPATH`` included, intact.

    ``None`` when this module has no file behind it (a frozen build): the child
    then inherits the environment unchanged, which is the best that can be said.
    """
    location = globals().get("__file__")
    if not isinstance(location, str) or not location:
        return None
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(location)))
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_root}{os.pathsep}{inherited}" if inherited else package_root
    return env


def _parse_core_options(raw: object) -> dict[str, CoreOption] | None:
    """Parse a probe's / fixture's option map — ``None`` (not captured) stays ``None``.

    A malformed entry is dropped rather than invented; a wholly malformed map
    counts as not captured.
    """
    if not isinstance(raw, dict):
        return None
    options: dict[str, CoreOption] = {}
    for key, spec in raw.items():
        if not isinstance(key, str) or not key or not isinstance(spec, dict):
            continue
        default = spec.get("default")
        values = spec.get("values")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            values = []
        options[key] = CoreOption(
            key=key,
            default=default if isinstance(default, str) else None,
            values=tuple(values),
        )
    return options


# Fixture file specs: a plain string is readable content; an object states a
# non-ok read outcome ({"status": "unreadable"} or {"status": "invalid-text"})
# or a binary blob's identity ({"md5": ..., "sha1": ..., "size": ...}).
#
# A blob is how a fixture states a firmware file: firmware is not text, and its
# identity is exactly the size and digests a real one would answer with — so
# the fixture declares them rather than carrying bytes that would have to hash
# to a real dump's md5. A string-content file needs no declaration: its size
# and digests are computed from the content, so fixture and real machine agree
# by construction.
#
# The two are independent axes because the machine answers them from two
# different reads: ``size`` comes from the ``stat``, the digests from the bytes.
# A chmod-000 file was observed to answer file (``stat`` succeeded), unreadable,
# its real size, and no digest — so ``{"status": "unreadable", "size": N}`` is a
# state a fixture must be able to spell, and the size-less spelling keeps its
# own meaning: present, unreadable, and no size either, which is what a FIFO or
# a device node answers (``file_size`` holds for regular files only).
FixtureFileSpec = str | Mapping[str, str | int]

_BLOB_KEYS = ("size", *DIGEST_ALGORITHMS)


def _file_identity(path: str, spec: Mapping[str, str | int]) -> dict[str, str | int]:
    """The identity fields of one object spec — size from the stat, digests from the bytes.

    A digest alongside ``unreadable`` is refused rather than ignored: the bytes
    are precisely what cannot be read there, a real one answers ``None`` for
    ``file_digest``, and a fixture stating one would make a vector assert a
    ``checked`` verdict the machine it models never reaches.
    """
    identity = {key: spec[key] for key in _BLOB_KEYS if key in spec}
    if spec.get("status") == READ_UNREADABLE and any(key in identity for key in DIGEST_ALGORITHMS):
        raise ValueError(
            f"fixture file {path!r}: an unreadable file states no digest — its bytes are what cannot "
            "be read, so a real one answers None; only 'size' survives, because it comes from the stat"
        )
    return identity


def _index_fixture_files(
    files: Mapping[str, FixtureFileSpec],
) -> tuple[dict[str, tuple[ReadStatus, str | None]], dict[str, dict[str, str | int]]]:
    """Split the declared file specs into read outcomes and declared identities.

    A malformed spec raises rather than being coerced: a fixture that cannot be
    read as written would otherwise prove something about a machine nobody
    described.
    """
    read: dict[str, tuple[ReadStatus, str | None]] = {}
    blobs: dict[str, dict[str, str | int]] = {}
    for path, spec in files.items():
        if isinstance(spec, str):
            read[path] = (READ_OK, spec)
            continue
        status = spec.get("status")
        identity = _file_identity(path, spec)
        if status is None:
            if not identity:
                raise ValueError(
                    f"fixture file {path!r}: an object spec must carry a 'status' or at least one "
                    f"of {list(_BLOB_KEYS)}"
                )
            # A blob exists and is not text — the same answer a real
            # firmware file gives read_text.
            read[path] = (READ_INVALID_TEXT, None)
        elif status in (READ_UNREADABLE, READ_INVALID_TEXT):
            read[path] = (status, None)
        else:
            raise ValueError(f"fixture file {path!r}: status must be 'unreadable' or 'invalid-text'")
        if identity:
            blobs[path] = identity
    return read, blobs


@dataclass(frozen=True, slots=True)
class _Landing:
    """Where a spelled path lands, or which refusal the kernel answers instead.

    ``path`` is set exactly when the walk completed. The two refusals are held
    apart because the seam answers them differently: a chain that never settles
    is ``ELOOP``, a *failing stat*, which every operation reports as
    inaccessible; a component the walk must step through that is not a
    directory is ``ENOTDIR``, which ``os.stat`` raises as ``NotADirectoryError``
    and :class:`RealMachine` reports as missing (observed: a regular file
    spelled with a trailing slash answers missing, not inaccessible).
    """

    path: str | None = None
    loops: bool = False


_LOOPS = _Landing(loops=True)
_NOT_A_DIRECTORY = _Landing()


def _refuse_both_unreadable_lists(inaccessible: set[str], unlistable: set[str]) -> None:
    """A path cannot both fail its ``stat`` and be a directory whose ``stat`` succeeded.

    Refused rather than resolved, for the same reason a digest alongside
    ``unreadable`` is: a precedence rule here would be a documented silent
    degradation, and this contradiction has a tempting wrong reading — that
    both lists together spell a mode-000 directory. They do not. Such a
    directory answers *directory* about itself, so it belongs in ``unlistable``
    alone, and any precedence would quietly hand back the machine the fixture
    was not describing.
    """
    both = sorted(inaccessible & unlistable)
    if both:
        raise ValueError(
            f"fixture paths {both} are in both 'inaccessible' and 'unlistable' — a path whose stat "
            "fails cannot also be a directory whose stat succeeds. A mode-000 directory is "
            "'unlistable'; name its children in 'inaccessible' if they matter."
        )


def _ancestor_dirs(paths: Iterable[str]) -> set[str]:
    """Every ancestor of the given paths — a known path's parents are directories."""
    dirs: set[str] = set()
    for path in paths:
        parent = os.path.dirname(path)
        while parent and parent != "/":
            dirs.add(parent)
            parent = os.path.dirname(parent)
    return dirs


# The whole-archive states a fixture AppImage may declare, and the per-entry
# ones: what RealMachine can report short of an entry's text. "missing" is not
# among them — an absent AppImage is modeled by not declaring the path at all,
# and an absent entry by not declaring the entry.
_FIXTURE_APPIMAGE_STATES = ("unreadable", "not-appimage", "capability-missing")
_FIXTURE_APPIMAGE_ENTRY_STATES = ("unreadable", "invalid-text")


def _validate_fixture_appimages(
    appimages: Mapping[str, Mapping[str, object] | str],
) -> dict[str, dict[str, object] | str]:
    validated: dict[str, dict[str, object] | str] = {}
    for path, spec in appimages.items():
        if isinstance(spec, str):
            if spec not in _FIXTURE_APPIMAGE_STATES:
                raise ValueError(
                    f"appimage {path!r}: a whole-archive state must be one of "
                    f"{_FIXTURE_APPIMAGE_STATES}, got {spec!r}"
                )
            validated[path] = spec
            continue
        entries: dict[str, object] = {}
        for inner, value in spec.items():
            if isinstance(value, str):
                entries[inner] = value
                continue
            if (
                isinstance(value, Mapping)
                and set(value) == {"status"}
                and value["status"] in _FIXTURE_APPIMAGE_ENTRY_STATES
            ):
                entries[inner] = dict(value)
                continue
            raise ValueError(
                f"appimage {path!r} entry {inner!r}: expected text or "
                f"{{'status': one of {_FIXTURE_APPIMAGE_ENTRY_STATES}}}, got {value!r}"
            )
        validated[path] = entries
    return validated


# The states a fixture PS2 BIOS header may declare short of the two strings:
# what RealMachine can report about a file it opened. "missing" is not among
# them — an absent file is modeled by not declaring it in ``files``.
_FIXTURE_PS2_BIOS_STATES = ("unreadable", "not-a-bios")
_FIXTURE_PS2_BIOS_FIELDS = ("romver", "serial")


def _validate_fixture_ps2_bios_headers(
    headers: Mapping[str, Mapping[str, object] | str],
    files: Mapping[str, tuple[ReadStatus, str | None]],
) -> dict[str, ps2_bios.Ps2BiosHeader | str]:
    """The declared header answers, built once by the reader's own code.

    A header describes a file's bytes, so its path must be a declared file,
    and one whose bytes can be read: a header beside ``unreadable`` would let
    a vector assert a read the machine it models never completes — the same
    refusal ``files`` makes for a digest on an unreadable file.
    """
    validated: dict[str, ps2_bios.Ps2BiosHeader | str] = {}
    for path, spec in headers.items():
        if path not in files:
            raise ValueError(
                f"ps2 bios header {path!r}: a header describes a file's bytes, and no file is declared there"
            )
        if files[path][0] == READ_UNREADABLE:
            raise ValueError(
                f"ps2 bios header {path!r}: an unreadable file states no header answer — its bytes are "
                "what cannot be read, so a real one answers 'unreadable' before any test"
            )
        if isinstance(spec, str):
            if spec not in _FIXTURE_PS2_BIOS_STATES:
                raise ValueError(
                    f"ps2 bios header {path!r}: a state must be one of {_FIXTURE_PS2_BIOS_STATES}, got {spec!r}"
                )
            validated[path] = spec
            continue
        validated[path] = _fixture_ps2_bios_header(path, spec)
    return validated


def _fixture_ps2_bios_header(path: str, spec: Mapping[str, object]) -> ps2_bios.Ps2BiosHeader:
    """The two strings the ROMDIR walk yields, checked to the lengths the core reads them at."""
    if set(spec) != set(_FIXTURE_PS2_BIOS_FIELDS):
        raise ValueError(
            f"ps2 bios header {path!r}: expected a state or an object with exactly "
            f"{_FIXTURE_PS2_BIOS_FIELDS}, got {spec!r}"
        )
    strings: dict[str, bytes] = {}
    for key in _FIXTURE_PS2_BIOS_FIELDS:
        value = spec[key]
        if not isinstance(value, str):
            raise ValueError(f"ps2 bios header {path!r}: {key} must be a string, got {value!r}")
        try:
            strings[key] = value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError(
                f"ps2 bios header {path!r}: {key} must be one byte per character (latin-1), got {value!r}"
            ) from error
    if len(strings["romver"]) != ps2_bios.ROMVER_LENGTH:
        raise ValueError(
            f"ps2 bios header {path!r}: romver is the {ps2_bios.ROMVER_LENGTH} bytes the core reads, "
            f"got {len(strings['romver'])}"
        )
    if len(strings["serial"]) > ps2_bios.SERIAL_LENGTH or b"\x00" in strings["serial"]:
        raise ValueError(
            f"ps2 bios header {path!r}: serial is at most {ps2_bios.SERIAL_LENGTH} bytes and carries no NUL "
            "— what a C string read from fifteen bytes can hold"
        )
    return ps2_bios.Ps2BiosHeader.from_strings(strings["romver"], strings["serial"])


class FixtureMachine:
    """A machine backed by plain data: files, directories, symlinks, core answers.

    ``files`` maps absolute paths to contents — a string is readable content;
    ``{"status": "unreadable"}`` / ``{"status": "invalid-text"}`` is a file
    that exists but yields that read outcome; ``{"md5": ..., "sha1": ...,
    "size": ...}`` is a binary blob that exists, reads as ``invalid-text``, and
    answers those values for ``file_digest`` / ``file_size``. A ``size`` may
    join a ``status``, because the two come from different reads on a real
    machine: ``{"status": "unreadable", "size": N}`` is the chmod-000 file,
    whose ``stat`` succeeds while its bytes do not. ``dirs`` lists directories
    that exist explicitly (parents of every known path are directories
    implicitly — the list is how *empty* directories are stated). ``symlinks``
    maps link paths to their targets (absolute, or relative to the link's
    directory). ``cores`` maps ``.so`` paths to core-answer objects
    (``{"library_name": ...}``); a path mapped to ``None`` is a core that is
    present but unloadable. ``appimages`` and ``ps2_bios_headers`` model two
    content reads at the seam rather than as bytes — see
    :meth:`read_appimage_text` and :meth:`read_ps2_bios_header`.

    Two lists say what cannot be read, and which one applies is decided by one
    question: does the ``stat`` succeed?

    - ``unlistable`` — it does. The path *is* a directory and its contents
      cannot be read, which is what **a mode-000 directory answers about
      itself**: ``stat`` succeeds, so it is a directory, and the listing fails.
      Mode 111 behaves the same from outside, and shows the shape plainly — a
      wildcard finds nothing there while a name atlas already knows still
      answers. This is the list the resolver's failure paths run through,
      because reaching them means passing an "is it a directory?" check first.
    - ``inaccessible`` — it does not, so nothing about the path can be told.
      This is what the paths *below* a mode-000 directory answer, and what a
      mount point answers once the card behind it stops responding (``EIO`` on
      the stat). Declaring a directory declares its whole subtree, because the
      failing ``stat`` is the one every path below it needs first.

    So a mode-000 directory is ``unlistable``, and it is the wrong list that is
    tempting: putting the directory in ``inaccessible`` makes the directory
    itself unreachable, and a resolver then refuses it long before it would
    have tried to read it. Naming a path in both is not a mode-000 shorthand
    but a contradiction, and it fails construction rather than resolving to
    either — a precedence rule would be a documented way to describe the wrong
    machine quietly. Where a mode-000 directory's children matter, they are
    named in ``inaccessible`` individually; the directory itself is not.

    Every operation resolves the path it is given the way the module docstring
    states the kernel does — symlink components, ``.``, ``..`` and separator
    spellings alike — because the store is keyed by one path per file while a
    machine answers for every spelling that reaches it. So a link to a target
    that is not in the fixture is a *dead* link (``readlink`` shows it,
    ``path_kind`` says missing, exactly like the real thing), and a fixture
    cannot answer *missing* for a file the real machine hands over under a
    ``.``, a ``..``, a doubled separator or a trailing slash.
    """

    def __init__(
        self,
        files: Mapping[str, FixtureFileSpec],
        symlinks: Mapping[str, str] | None = None,
        cores: Mapping[str, Mapping[str, object] | None] | None = None,
        dirs: Iterable[str] | None = None,
        inaccessible: Iterable[str] | None = None,
        unlistable: Iterable[str] | None = None,
        appimages: Mapping[str, Mapping[str, object] | str] | None = None,
        ps2_bios_headers: Mapping[str, Mapping[str, object] | str] | None = None,
    ) -> None:
        self._files, self._blobs = _index_fixture_files(files)
        self._symlinks = dict(symlinks or {})
        self._cores = dict(cores or {})
        self._appimages = _validate_fixture_appimages(appimages or {})
        self._ps2_bios_headers = _validate_fixture_ps2_bios_headers(ps2_bios_headers or {}, self._files)
        self._inaccessible = set(inaccessible or ())
        self._unlistable = set(unlistable or ())
        _refuse_both_unreadable_lists(self._inaccessible, self._unlistable)
        # A directory whose contents cannot be read is still a directory —
        # that is the whole difference from an inaccessible one, and what lets
        # a resolver walk up to it and then fail to look inside.
        self._dirs: set[str] = set(dirs or ()) | self._unlistable
        # Every ancestor of a known path is a directory.
        self._dirs |= _ancestor_dirs(
            (*self._files, *self._symlinks, *self._cores, *tuple(self._dirs), *self._inaccessible)
        )
        self._dirs.discard("")
        # The root is a directory on every machine, and no ancestor walk ever
        # reaches it (they stop at "/"). Without it a fixture answers *missing*
        # for "/" and for any spelling that climbs to it, and the walk below
        # relies on it: standing at "/" has to be standing in a directory.
        self._dirs.add("/")

    def _resolve(self, path: str) -> _Landing:
        """Walk *path* component by component from ``/``, the way the kernel does.

        The one rule the failure spellings fall out of: before the walk steps
        *through* a place, that place must be a directory. So ``f.txt/`` and
        ``f.txt/../g`` are ``ENOTDIR`` while ``dir/`` and ``dir/./`` are the
        directory, and a ``..`` climbs from where the walk landed rather than
        from the spelling — ``link/..`` is the link target's parent.

        This is a resolution, not a normalization: collapsing the spelling
        first (``os.path.normpath``) would eat the component in front of a
        ``..`` even when that component is a symlink, and the kernel does the
        opposite. Nothing here consults the *final* component's kind; that is
        the caller's question, and a dangling final component is a dead link,
        not a refusal.

        A relative path names nothing: the real machine resolves it against the
        working directory of the process, which is not a fact about the machine
        being described, so a fixture has nowhere to start. (One reaches here —
        ``system_directory = "system"`` is checked for being a directory before
        it is refused for not being absolute.)

        This walk exists three times, for three different jobs:
        :func:`atlas.firmware.resolve_links` resolves a path *through* the seam
        for a caller that then reads it, and
        ``atlas.installations._resolve_symlink_chain`` also collects the links
        it traversed so a caveat can name them. They deliberately differ — only
        this one refuses to step through a non-directory, because only this one
        answers for the paths themselves — but a fidelity finding about
        symlinks, ``..`` or the hop limit belongs in all three, and they share
        :data:`SYMLINK_HOPS` so the boundary cannot drift.
        """
        if not path.startswith("/"):
            return _NOT_A_DIRECTORY
        parts = path.split("/")[1:]
        resolved = "/"
        hops = 0
        while parts:
            if resolved not in self._dirs:
                return _NOT_A_DIRECTORY
            segment = parts.pop(0)
            if segment in ("", "."):
                continue
            if segment == "..":
                resolved = os.path.dirname(resolved) or "/"
                continue
            candidate = os.path.join(resolved, segment)
            target = self._symlinks.get(candidate)
            if target is None:
                resolved = candidate
                continue
            hops += 1
            if hops > SYMLINK_HOPS:
                return _LOOPS
            if target.startswith("/"):
                resolved = "/"
            # A relative target is relative to the directory holding the link,
            # which is exactly where `resolved` already stands.
            parts = [p for p in target.split("/") if p and p != "."] + parts
        return _Landing(resolved)

    def _resolve_parent(self, path: str) -> str | None:
        """Resolve symlinks in the parent components only — the final one stays.

        ``None`` when the spelling names no final component to report on: a
        trailing ``/``, ``.`` or ``..`` all make the kernel follow the last
        component instead of naming it, which is why a link to a directory
        answers its target when spelled bare and ``None`` when spelled with a
        trailing slash (observed on both).
        """
        parent, name = os.path.dirname(path), os.path.basename(path)
        if name in ("", ".", ".."):
            return None
        if parent in ("", "/"):
            return path
        landing = self._resolve(parent)
        return None if landing.path is None else os.path.join(landing.path, name)

    def read_appimage_text(self, path: str, inner_path: str) -> AppImageReadResult:
        """The modeled AppImage read — data in, the same outcomes RealMachine reports.

        Modeling lives beside the seam rather than in ``files``: what a real
        AppImage read needs (ELF layout, squashfs walk, a codec) is exactly
        what a fixture must NOT need, or every vector would carry a binary
        and only run where the codec exists. A path not declared here is a
        missing AppImage; an entry not declared is ``entry-missing``.
        """
        spec = self._appimages.get(path)
        if spec is None:
            return AppImageReadResult(READ_MISSING)
        if isinstance(spec, str):
            status: AppImageReadStatus = spec  # type: ignore[assignment]  # validated at construction
            return AppImageReadResult(status)
        entry = spec.get(inner_path)
        if entry is None:
            return AppImageReadResult(APPIMAGE_ENTRY_MISSING)
        if isinstance(entry, str):
            return AppImageReadResult(READ_OK, entry)
        entry_status: AppImageReadStatus = entry["status"]  # type: ignore[index,assignment]
        return AppImageReadResult(entry_status)

    def read_ps2_bios_header(self, path: str) -> Ps2BiosHeaderResult:
        """The modeled ROMDIR read — the two strings the walk yields in, the same outcomes RealMachine reports.

        Modeling lives beside the seam rather than in ``files`` for the reason
        ``appimages`` does: what the real read needs — a ROMDIR table inside a
        4 MiB blob — is exactly what a fixture must not carry. A path declared
        in ``ps2_bios_headers`` answers its header, built by the reader's own
        code from the ``romver`` and ``serial`` strings, or the state it
        names. A declared file with no entry there answers ``missing``: the
        fixture has no header to answer with, and answering ``not-a-bios``
        instead would let a vector that forgot the key prove a verdict nobody
        modeled — the folder route treats ``missing`` for a file it has just
        listed as a read that did not happen. An ``unreadable`` file answers
        ``unreadable`` before any test, as the real one does.
        """
        if self._is_inaccessible(path):
            return Ps2BiosHeaderResult(PS2_BIOS_UNREADABLE)
        resolved = self._resolve(path).path
        if resolved is None:
            return Ps2BiosHeaderResult(PS2_BIOS_MISSING)
        spec = self._ps2_bios_headers.get(resolved)
        if isinstance(spec, str):
            status: Ps2BiosHeaderStatus = spec  # type: ignore[assignment]  # validated at construction
            return Ps2BiosHeaderResult(status)
        if spec is not None:
            return Ps2BiosHeaderResult(PS2_BIOS_OK, spec)
        if resolved in self._dirs or self._files.get(resolved, (READ_MISSING, None))[0] == READ_UNREADABLE:
            return Ps2BiosHeaderResult(PS2_BIOS_UNREADABLE)
        return Ps2BiosHeaderResult(PS2_BIOS_MISSING)

    def read_text(self, path: str) -> ReadResult:
        if self._is_inaccessible(path):
            return ReadResult(READ_UNREADABLE)
        resolved = self._resolve(path).path
        if resolved is None:
            # ENOTDIR — the loop case was already answered above. The real
            # machine raises NotADirectoryError here, which it reports missing.
            return ReadResult(READ_MISSING)
        if resolved in self._files:
            status, text = self._files[resolved]
            return ReadResult(status, text)
        if resolved in self._cores:
            return ReadResult(READ_INVALID_TEXT)  # a binary is not text
        if resolved in self._dirs:
            return ReadResult(READ_UNREADABLE)
        return ReadResult(READ_MISSING)

    def path_kind(self, path: str) -> PathKind:
        if self._is_inaccessible(path):
            return KIND_INACCESSIBLE
        resolved = self._resolve(path).path
        if resolved is None:
            return KIND_MISSING  # ENOTDIR, as above
        if resolved in self._files or resolved in self._cores:
            return KIND_FILE
        if resolved in self._dirs:
            return KIND_DIRECTORY
        return KIND_MISSING

    def _is_inaccessible(self, path: str) -> bool:
        if self._at_or_under_inaccessible(path):
            return True
        landing = self._resolve(path)
        # A chain that never settles is ELOOP: the real machine fails to stat
        # it, which every operation here reports as inaccessible. ENOTDIR is
        # not that — it is an absent path, and each operation says so itself.
        return landing.loops or (
            landing.path is not None and self._at_or_under_inaccessible(landing.path)
        )

    def _at_or_under_inaccessible(self, path: str) -> bool:
        """Is *path* a declared inaccessible entry, or anything below one?

        Declaring a directory inaccessible declares its whole subtree: the
        ``stat`` that fails on it is the one every path below it needs first,
        and a mode-000 directory was observed to answer *inaccessible* for its
        children and its grandchildren alike. Stating each descendant instead
        would mean listing what a broken card contains in order to say it
        cannot be read.
        """
        return any(
            path == entry or path.startswith(entry.rstrip("/") + "/") for entry in self._inaccessible
        )

    def readlink(self, path: str) -> str | None:
        parent = self._resolve_parent(path)
        return None if parent is None else self._symlinks.get(parent)

    def file_size(self, path: str) -> int | None:
        if self._is_inaccessible(path):
            return None
        resolved = self._resolve(path).path
        if resolved is None:
            return None
        declared = self._blobs.get(resolved, {}).get("size")
        if isinstance(declared, int):
            return declared
        text = self._files.get(resolved, (READ_MISSING, None))[1]
        return len(text.encode("utf-8")) if text is not None else None

    def file_digest(self, path: str, algorithm: str) -> str | None:
        if algorithm not in DIGEST_ALGORITHMS or self._is_inaccessible(path):
            return None
        resolved = self._resolve(path).path
        if resolved is None:
            return None
        declared = self._blobs.get(resolved, {}).get(algorithm)
        if isinstance(declared, str):
            return declared
        text = self._files.get(resolved, (READ_MISSING, None))[1]
        if text is None:
            return None
        return hashlib.new(algorithm, text.encode("utf-8")).hexdigest()

    def query_core(self, so_path: str) -> CoreInfo | None:
        resolved = self._resolve(so_path).path
        spec = self._cores.get(resolved) if resolved is not None else None
        if not spec:
            return None
        name = spec.get("library_name")
        if not isinstance(name, str) or not name:
            return None
        version = spec.get("library_version")
        extensions = spec.get("valid_extensions")
        block_extract = spec.get("block_extract")
        return CoreInfo(
            library_name=name,
            library_version=version if isinstance(version, str) else None,
            valid_extensions=extensions if isinstance(extensions, str) else None,
            options=_parse_core_options(spec.get("options")),
            block_extract=block_extract if isinstance(block_extract, bool) else None,
        )

    def glob(self, pattern: str) -> GlobResult:
        return _GlobWalk(self._list_dir, self._is_dir, self._lexists).run(pattern)

    def _list_dir(self, path: str) -> list[str] | None:
        """The names directly under *path*, or ``None`` when it cannot be listed.

        A directory the fixture calls inaccessible is one whose ``stat`` fails,
        and nothing whose ``stat`` fails can be listed either — so the two
        answers come from the one declaration.
        """
        if self._is_inaccessible(path):
            return None
        landing = self._resolve(path)
        if landing.path is None or landing.path not in self._dirs:
            # Not there, or not a directory: a truthful empty listing.
            return []
        if landing.path in self._unlistable:
            return None
        prefix = landing.path.rstrip("/") + "/"
        names = {
            known[len(prefix) :].split("/", 1)[0]
            for known in (*self._files, *self._symlinks, *self._cores, *self._dirs, *self._inaccessible)
            if known.startswith(prefix)
        }
        names.discard("")
        return sorted(names)

    def _is_dir(self, path: str) -> bool | None:
        kind = self.path_kind(path)
        return None if kind == KIND_INACCESSIBLE else kind == KIND_DIRECTORY

    def _lexists(self, path: str) -> bool | None:
        # lstat semantics: a dead symlink is a name that exists.
        if self._is_inaccessible(path):
            return None
        if self.readlink(path) is not None:
            return True
        return self.path_kind(path) != KIND_MISSING
