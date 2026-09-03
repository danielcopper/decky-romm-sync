#!/usr/bin/env python3
"""Vendored emu-atlas integrity gate.

``py_modules/_vendor/atlas/`` is a verbatim copy of the ``atlas`` package from a
tagged `emu-atlas <https://github.com/danielcopper/emu-atlas>`_ wheel — there is
no source for it in this repo, so the only thing separating "the pinned release"
from "whatever happens to be on disk" is a checksum. The copy is also large
enough (58 files) that reviewing it as a diff reads as noise, which is exactly
the condition under which a hand-edit or a half-finished re-copy survives review.

This gate asserts three things against ``_vendor/atlas.SHA256SUMS`` — upstream's
own release manifest, vendored verbatim beside the tree:

  * every ``atlas/…`` entry in the manifest matches the vendored file's digest;
  * ``_vendor/atlas.LICENSE`` matches the manifest's dist-info licence entry
    (the licence is vendored as a SIBLING of the tree, not inside it, so the
    copied tree stays exactly equal to the manifest and this check needs no
    per-file exceptions);
  * the vendored file set EQUALS the manifest's set — nothing missing, nothing
    added.

That third assertion is why this is a script and not a one-line ``sha256sum -c``
in ``mise.toml``. Neither form of that command works here: the manifest carries
nine entries that are never vendored (three release artifacts and six dist-info
files), so the plain form fails even on a perfect copy, and ``--ignore-missing``
— which is what makes it green again — exits 0 after a vendored file is deleted,
because a deleted file is simply skipped. Digests are computed here in Python
rather than shelled out to ``sha256sum``, whose result text is localised (a
mismatch prints ``FEHLSCHLAG`` on a German machine) — branching on it would make
the gate pass or fail by locale.

``__pycache__`` is ignored: it is a build artifact of importing the tree, never
part of the manifest, and it is not committed.

Exit 0 when the copy is exactly the pinned release, 1 (one line per discrepancy,
plus the re-copy procedure) otherwise.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "py_modules" / "_vendor"

MANIFEST_NAME = "atlas.SHA256SUMS"
LICENSE_NAME = "atlas.LICENSE"

# The vendored tree's root inside the manifest. Upstream's manifest also covers
# the dist-info and the release tarballs; only this prefix is vendored.
TREE_PREFIX = "atlas/"

# The manifest's licence entry, matched by shape rather than by literal name so
# a version bump is a manifest re-copy and not a script edit. Exactly one entry
# must match — see :func:`licence_entry`.
LICENSE_ENTRY_RE = re.compile(r"^emu_atlas-[^/]+\.dist-info/licenses/LICENSE$")

# A ``sha256sum`` manifest line: ``<64 hex><two spaces or space-star><path>``.
# The ``*`` marks binary mode; upstream writes text mode, but accepting both
# costs nothing and avoids a silently unparsed line.
_ENTRY_RE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


def parse_manifest(text: str, source: str = MANIFEST_NAME) -> dict[str, str]:
    """Map path -> expected digest for every entry in a ``sha256sum`` manifest.

    Blank lines are skipped; anything else that does not parse raises, because
    silently dropping a line would silently drop a check.
    """
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        match = _ENTRY_RE.match(raw)
        if match is None:
            raise SystemExit(f"{source}: unparseable line {lineno}: {raw!r}")
        digest, path = match.group(1), match.group(2)
        if path in entries:
            raise SystemExit(f"{source}: duplicate entry for {path!r} on line {lineno}")
        entries[path] = digest
    return entries


def licence_entry(manifest: dict[str, str], source: str = MANIFEST_NAME) -> tuple[str, str]:
    """The manifest's dist-info licence entry, as ``(path, digest)``.

    Requires exactly one match: none means the manifest is not a wheel's, and
    several would make the licence check pick one arbitrarily.
    """
    matches = sorted(path for path in manifest if LICENSE_ENTRY_RE.match(path))
    if len(matches) != 1:
        raise SystemExit(f"{source}: expected exactly one dist-info licence entry, found {len(matches)}")
    return matches[0], manifest[matches[0]]


def digest_of(path: pathlib.Path) -> str:
    """SHA-256 of ``path``, as lowercase hex."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def vendored_tree_files(vendor_dir: pathlib.Path) -> set[str]:
    """Every vendored file, as a manifest-relative POSIX path (``atlas/…``).

    A symlink is not a file here. ``is_file()`` follows one, so without the
    guard a symlink standing where a manifest file belongs would be digested
    through to its target and pass; with it, the entry is reported missing.
    Upstream's wheel contains no symlinks, so one appearing is drift either way.

    The boundary this does NOT close: ``rglob`` does not descend into a
    symlinked directory, so files below one are invisible to the comparison
    whatever this check does. Against the drift this gate is for — a hand-edit,
    a half-finished re-copy — that does not arise, and git records a symlink as
    mode 120000, so it is visible in review rather than silent.
    """
    tree_root = vendor_dir / TREE_PREFIX.rstrip("/")
    return {
        path.relative_to(vendor_dir).as_posix()
        for path in tree_root.rglob("*")
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
    }


def collect_discrepancies(vendor_dir: pathlib.Path) -> list[str]:
    """Every way ``vendor_dir``'s copy differs from the release it pins, in stable order."""
    manifest_path = vendor_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"{MANIFEST_NAME}: missing — the vendored copy pins no release at all"]

    manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    expected = {path: digest for path, digest in manifest.items() if path.startswith(TREE_PREFIX)}
    if not expected:
        return [f"{MANIFEST_NAME}: no '{TREE_PREFIX}' entries — this is not an emu-atlas wheel manifest"]

    found = vendored_tree_files(vendor_dir)
    discrepancies: list[str] = [
        f"{path}: in the manifest but missing from the vendored tree" for path in sorted(expected.keys() - found)
    ]
    discrepancies.extend(
        f"{path}: in the vendored tree but not in the manifest" for path in sorted(found - expected.keys())
    )
    for path in sorted(expected.keys() & found):
        actual = digest_of(vendor_dir / path)
        if actual != expected[path]:
            discrepancies.append(f"{path}: digest {actual} != manifest {expected[path]}")

    entry_path, entry_digest = licence_entry(manifest)
    licence_copy = vendor_dir / LICENSE_NAME
    if not licence_copy.is_file():
        discrepancies.append(f"{LICENSE_NAME}: missing — upstream's licence, copied from {entry_path}")
    else:
        actual = digest_of(licence_copy)
        if actual != entry_digest:
            discrepancies.append(f"{LICENSE_NAME}: digest {actual} != manifest {entry_digest} ({entry_path})")

    return discrepancies


def main() -> int:
    discrepancies = collect_discrepancies(VENDOR_DIR)
    if discrepancies:
        print("ERROR: the vendored emu-atlas copy is not the release its manifest pins:", file=sys.stderr)
        for line in discrepancies:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nA vendored package is a verbatim copy — never hand-edit it, and never fix a\n"
            "problem here that belongs upstream. Re-copy from a tagged release instead:\n"
            "\n"
            "  gh release download <tag> -R danielcopper/emu-atlas \\\n"
            "      -p 'emu_atlas-*-py3-none-any.whl' -p SHA256SUMS -D <tmp>\n"
            "  (cd <tmp> && sha256sum -c --ignore-missing SHA256SUMS)   # verify the wheel\n"
            "  unzip -d <tmp>/u <tmp>/emu_atlas-*-py3-none-any.whl\n"
            "\n"
            "then replace py_modules/_vendor/atlas/ with <tmp>/u/atlas/ (no __pycache__),\n"
            f"copy the dist-info licence to py_modules/_vendor/{LICENSE_NAME} and SHA256SUMS\n"
            f"to py_modules/_vendor/{MANIFEST_NAME}.\n"
            "\n"
            "Full procedure, and the version to bump: py_modules/_vendor/README.md",
            file=sys.stderr,
        )
        return 1
    print("OK: the vendored emu-atlas copy matches its pinned release manifest exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
