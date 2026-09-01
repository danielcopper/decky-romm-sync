"""How RetroArch names the content it was given — the port of ``runloop_path_set_basename``.

Every save-path decision is made from one string, ``runtime_content_path_basename``,
built once per load in ``runloop_path_set_basename`` (``runloop.c:8673-8713``):
the content directory is ``fill_pathname_basedir`` of it (``runloop.c:8789``),
the sort-by-content component ``fill_pathname_parent_dir_name`` of it
(``runloop.c:8781``), and the save file is its last component with ``.srm``
appended — ``fill_pathname_dir`` in ``runloop_path_set_redirect``
(``runloop.c:8929-8936``), which is the naming that governs wherever the save
path is a directory, i.e. everywhere atlas answers. The earlier
``fill_pathname(basename, ".srm")`` in ``runloop_path_set_names``
(``runloop.c:8720``) is *not* the rule to port: it truncates a second extension
off the stem (``Game.v1.1`` would become ``Game.v1.srm``) and the redirect
overwrites its result. Naming the content wrongly therefore moves the directory
*and* the file name at once — so this is a port of the upstream math, not an
approximation of it (evidence: ``docs/research/retrodeck-save-placement.md`` §4).

Two rules are worth knowing before reading the code:

- **Content inside an archive is named after the entry.** ``path_basedir_wrapper``
  cuts at the archive delimiter and keeps the directory (``file_path.c:1322-1341``),
  ``path_basename`` returns everything after it (``file_path.c:692-700``), and only
  then is the extension truncated — so ``/roms/n64/pack.zip#Game.n64`` is the ROM
  ``Game`` in ``/roms/n64``.
- **The extension is truncated on the whole path.** The guard is "the dot is not
  at index 0" (``runloop.c:8710-8711``), not "the basename begins with a dot", so
  a dot in a directory name truncates there when the ROM has no extension of its
  own: ``/roms/My.Games/rom`` is named ``/roms/My``.

Pure compute, stdlib only.
"""

from __future__ import annotations

import os

# Content inside an archive is spelled "<archive>#<entry>", and only a '#' right
# after one of these extensions starts the entry — '#' is a legal character in a
# file name otherwise. The shipped RetroArch reads that spelling: HAVE_COMPRESSION
# is set unconditionally by the build system (Makefile.common:1988) and the
# installed binary reports "7zip extraction support: yes" / "zip extraction
# support: yes" under --features.
_ARCHIVE_SUFFIXES = ("zip", "zst", "apk")


def _ascii_lower(text: str) -> str:
    """The source's ``c | 0x20`` folding — ASCII letters only, byte for byte."""
    return "".join(chr(ord(c) | 0x20) for c in text)


def _is_one_byte_at_most(path: str) -> bool:
    """Upstream's ``s[0] == '\\0' || s[1] == '\\0'`` — a test on bytes, not characters.

    ``path_basedir`` and ``path_basedir_wrapper`` both leave such a path alone.
    One non-ASCII character is one Python character but two or more UTF-8 bytes,
    so the length has to be measured the way the C sees it; undecodable bytes
    travel through Python paths as surrogates and are measured back the same way.
    """
    return len(path) < 2 and len(path.encode("utf-8", "surrogateescape")) < 2


def archive_delimiter(path: str) -> int:
    """Index of the ``#`` that separates an archive from the content inside it, or -1.

    ``path_get_archive_delim`` (``file_path.c:172-220``): the first ``#`` preceded
    by a known compression extension wins, the extension's letters compare
    case-insensitively, its dot exactly, and at least one character must precede
    that dot — so ``a.zip#rom`` splits and ``.zip#rom`` does not.
    """
    at = path.find("#")
    while at != -1:
        if at > 3 and path[at - 3 : at - 1] == ".7" and _ascii_lower(path[at - 1]) == "z":
            return at
        if at > 4 and path[at - 4] == "." and _ascii_lower(path[at - 3 : at]) in _ARCHIVE_SUFFIXES:
            return at
        at = path.find("#", at + 1)
    return -1


def _basename_after_delimiter(path: str) -> str:
    """``path_basename`` (``file_path.c:692-700``) — after the delimiter, else the last slash."""
    cut = archive_delimiter(path)
    if cut == -1:
        cut = path.rfind("/")
    return path[cut + 1 :]


def _basedir(path: str) -> str:
    """``path_basedir_wrapper`` (``file_path.c:1322-1341``) — the directory, trailing slash kept.

    The archive delimiter is cut *first*, so the directory of
    ``/roms/n64/pack.zip#Game.n64`` is the archive's own ``/roms/n64/``.
    """
    if _is_one_byte_at_most(path):
        return path
    cut = archive_delimiter(path)
    truncated = path[:cut] if cut != -1 else path
    slash = truncated.rfind("/")
    return truncated[: slash + 1] if slash != -1 else "./"


def _path_basedir(path: str) -> str:
    """``path_basedir`` (``file_path.c:625-640``) — up to and including the last slash.

    A name with no slash in it is the current directory's (``./``), and a
    one-byte path is left alone by the same early return upstream takes.
    """
    if _is_one_byte_at_most(path):
        return path
    slash = path.rfind("/")
    return path[: slash + 1] if slash != -1 else "./"


def _parent_dir_name(path: str) -> str:
    """``fill_pathname_parent_dir_name`` (``file_path.c:493-534``) — the last component's directory.

    A trailing slash is skipped first, so the last component is always read as
    the file name: ``/roms/gba/x`` and ``/roms/gba/x/`` both answer ``gba``.
    """
    named = path[:-1] if path.endswith("/") else path
    return named.rpartition("/")[0].rpartition("/")[2]


def content_basename(content_path: str) -> str:
    """RetroArch's ``runtime_content_path_basename`` for this content path.

    The port of ``runloop_path_set_basename`` (``runloop.c:8673-8713``): rebuild
    the path from the archive-aware directory and name, then truncate at the
    last dot unless it is the path's first character.
    """
    basename = _basedir(content_path)
    if basename:
        if not basename.endswith("/"):
            basename += "/"  # fill_pathname_slash, file_path.c:395-417
        basename += _basename_after_delimiter(content_path)
    dot = basename.rfind(".")
    return basename[:dot] if dot > 0 else basename


def split_content_path(content_path: str) -> tuple[str, str, str]:
    """Derive ``(content_dir_path, content_dir_name, rom_stem)`` from a content path.

    All three come off :func:`content_basename`, the value RetroArch's own path
    math reads. ``content_dir_path`` is :func:`_path_basedir` without its
    trailing slash — atlas states directories without one — so a relative
    content path answers ``.``, the directory upstream names, rather than the
    empty string ``os.path.dirname`` would give.

    A trailing slash needs no rule of its own: it disappears in that math, so
    ``/roms/psx/Game.cue/`` names ``Game`` in ``/roms/psx``, exactly as the same
    path without it. A path whose last component is empty *and* carries no dot
    leaves the stem empty — RetroArch would write a save called ``.srm`` there —
    and the caller states that rather than naming a file.
    """
    basename = content_basename(content_path)
    basedir = _path_basedir(basename)
    dir_path = basedir[:-1] if len(basedir) > 1 and basedir.endswith("/") else basedir
    return dir_path, _parent_dir_name(basename), os.path.basename(basename)


def content_system_dir(content_path: str) -> str:
    """The directory a core is handed as its *system* directory for this content.

    ``RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY`` answers with the content's own
    directory whenever ``systemfiles_in_content_dir`` is set or no
    ``system_directory`` is left standing, and it composes that directory here:
    ``fill_pathname_basedir`` of the **raw** content path (``runloop.c:1977``,
    ``file_path.c:475-480`` → :func:`_path_basedir`), then the trailing slash
    removed unless the result is the root directory — upstream tests that by
    counting slashes, so ``/rom.bin`` keeps its ``/`` and ``rom.bin`` keeps its
    ``./`` (``runloop.c:1979-1981``).

    Deliberately *not* :func:`split_content_path`'s directory: that one is the
    basedir of ``runtime_content_path_basename``, which the archive delimiter
    and the extension truncation have already been through. The two differ for
    content inside an archive subdirectory (``pack.zip#sub/rom.n64``, whose
    entry keeps its archive spelling here) and for a path written with a
    trailing slash. Each is the directory its own upstream caller computes, and
    a core asking for the system directory is answered by this one.
    """
    based = _path_basedir(content_path)
    if based.count("/") > 1 and based.endswith("/"):
        return based[:-1]
    return based


def content_file_name(content_path: str) -> str:
    """The name of the file the content path names *on disk*.

    For an archive-delimited path that is the archive itself: what lies behind
    the ``#`` is inside it, not next to it (``file_path.c:172-220``). Trailing
    slashes are dropped first, because the equivalence
    :func:`split_content_path` states for the *name* has to hold for the file:
    the same content named with and without one is the same file, and a
    placement that filters the content out of its observation must filter it
    either way.
    """
    cut = archive_delimiter(content_path)
    on_disk = content_path[:cut] if cut != -1 else content_path
    return os.path.basename(on_disk.rstrip("/"))
