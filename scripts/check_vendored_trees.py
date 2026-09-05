#!/usr/bin/env python3
"""Vendored-tree integrity gate.

Every package under ``py_modules/_vendor/`` is a copy of code we do not own and
have no source for in this repo — verbatim, or verbatim plus a documented local
patch — pinned by its own ``<pkg>.SHA256SUMS``, so the only thing separating
"the copy we pinned" from "whatever happens to be on disk" is a checksum. A tree
is also large enough that reviewing it as a diff reads as noise, which is exactly
the condition under which a hand-edit or a half-finished re-copy survives review.

The contract is one manifest per tree: ``<pkg>.SHA256SUMS`` beside ``<pkg>/``
pins it, and a package directory with no manifest is a failure. That second half
is what makes the next vendored package guarded by default rather than guarded
if someone remembers — nothing here is named after a package.

What a manifest IS differs per package, and does not change what is asserted:
for a tree taken verbatim from a release it is upstream's own manifest, so the
digests additionally prove identity with the tagged release; for a tree carrying
a deliberate local patch it is our own digest of the patched copy, so they prove
only that nobody has since reached into it. Which kind each one is, is recorded
per package in ``py_modules/_vendor/README.md``.

Three assertions per tree:

  * every ``<pkg>/…`` entry in the manifest matches the vendored file's digest;
  * the vendored file set EQUALS the manifest's set restricted to ``<pkg>/`` —
    nothing missing, nothing added;
  * the sibling ``<pkg>.LICENSE`` and the manifest's dist-info licence entry
    agree about each other. A wheel's manifest carries such an entry because the
    licence is then vendored BESIDE the tree, which is what keeps the tree
    exactly equal to the manifest; a manifest generated from a tree that holds
    its own licence file carries neither, and the tree digests cover that file
    instead. Having one without the other is reported both ways round: a sibling
    licence no entry pins is checked by nothing, and that is precisely what
    regenerating a wheel's manifest from its tree produces — the dist-info lines
    vanish and the licence assertion disappears with them while the file stays.

The set-equality assertion is why this is a script and not a one-line
``sha256sum -c`` in ``mise.toml``. Neither form of that command works here: a
wheel's manifest carries entries that are never vendored (release artifacts and
the dist-info), so the plain form fails even on a perfect copy, and
``--ignore-missing`` — which is what makes it green again — exits 0 after a
vendored file is deleted, because a deleted file is simply skipped. Digests are
computed here in Python rather than shelled out to ``sha256sum``, whose result
text is localised (a mismatch prints ``FEHLSCHLAG`` on a German machine) —
branching on it would make the gate pass or fail by locale.

``__pycache__`` is ignored wherever it appears, on BOTH sides of the comparison:
it is a build artifact of importing a tree, never part of a manifest, and it is
not committed. Filtering the manifest side too is what keeps the report honest —
a ``__pycache__`` line in a manifest would otherwise be reported as a file
missing from the vendored tree while it sits right there.

Exit 0 when every tree is exactly what its manifest pins, 1 (one line per
discrepancy, plus the re-copy procedure) otherwise.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "py_modules" / "_vendor"

MANIFEST_SUFFIX = ".SHA256SUMS"
LICENSE_SUFFIX = ".LICENSE"

PYCACHE = "__pycache__"

# A wheel's dist-info licence entry, matched by shape rather than by literal
# name so a version bump is a manifest re-copy and not a script edit. Both
# layouts are accepted — ``licenses/LICENSE`` since PEP 639, ``LICENSE`` before
# it — because a manifest whose shape went unrecognised would silently check
# one thing less. At most one entry may match; see :func:`licence_entry`.
LICENSE_ENTRY_RE = re.compile(r"^[^/]+\.dist-info/(?:licenses/)?LICENSE$")

# A ``sha256sum`` manifest line: ``<64 hex><two spaces or space-star><path>``.
# The ``*`` marks binary mode; upstream writes text mode, but accepting both
# costs nothing and avoids a silently unparsed line.
_ENTRY_RE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


class ManifestError(Exception):
    """Text that does not parse as a manifest, and so pins nothing it claims to.

    Carried as an exception rather than raised out of the process because the
    sweep visits every tree: an unparseable manifest is one package's finding,
    and exiting on it would hide every other package's drift and skip the
    remediation guidance the run ends with. A manifest that cannot be read at
    all — one the process cannot open, one whose bytes are not UTF-8 — never
    reaches the parser and so never becomes one of these;
    :func:`tree_discrepancies` answers for that half, to the same end.
    """


def parse_manifest(text: str, source: str) -> dict[str, str]:
    """Map path -> expected digest for every entry in a ``sha256sum`` manifest.

    ``source`` names the manifest in refusals. Blank lines are skipped; anything
    else that does not parse raises :class:`ManifestError`, because silently
    dropping a line would silently drop a check.
    """
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        match = _ENTRY_RE.match(raw)
        if match is None:
            raise ManifestError(f"{source}: unparseable line {lineno}: {raw!r}")
        digest, path = match.group(1), match.group(2)
        if path in entries:
            raise ManifestError(f"{source}: duplicate entry for {path!r} on line {lineno}")
        entries[path] = digest
    return entries


def licence_entry(manifest: dict[str, str], source: str) -> tuple[str, str] | None:
    """The manifest's dist-info licence entry as ``(path, digest)``, or ``None``.

    ``None`` means the manifest is not a wheel's, so upstream's licence is not
    vendored as a sibling — whether one is nonetheless sitting there is
    :func:`licence_discrepancies`'s question, not this one. Several matches
    raises :class:`ManifestError` rather than being resolved: picking one would
    decide arbitrarily which licence the sibling copy is held against.
    """
    matches = sorted(path for path in manifest if LICENSE_ENTRY_RE.match(path))
    if len(matches) > 1:
        raise ManifestError(f"{source}: expected at most one dist-info licence entry, found {len(matches)}")
    if not matches:
        return None
    return matches[0], manifest[matches[0]]


def in_pycache(path: str) -> bool:
    """Whether a manifest-relative POSIX path lies under a ``__pycache__`` directory."""
    return PYCACHE in path.split("/")


def digest_of(path: pathlib.Path) -> str:
    """SHA-256 of ``path``, as lowercase hex."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def manifest_paths(vendor_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """Package name -> its manifest, for every ``<pkg>.SHA256SUMS`` in ``vendor_dir``."""
    return {path.name[: -len(MANIFEST_SUFFIX)]: path for path in vendor_dir.glob(f"*{MANIFEST_SUFFIX}")}


def package_dirs(vendor_dir: pathlib.Path) -> set[str]:
    """Every vendored package in ``vendor_dir``, by directory name.

    A package is a directory. The top level's other entries — ``__init__.py``,
    ``README.md``, ``<pkg>.LICENSE``, ``<pkg>.SHA256SUMS`` — are files and pin
    nothing, so they are not asked for a manifest; ``__pycache__`` is the one
    directory that is not a package.
    """
    return {entry.name for entry in vendor_dir.iterdir() if entry.is_dir() and entry.name != PYCACHE}


def vendored_tree_files(vendor_dir: pathlib.Path, package: str) -> set[str]:
    """Every file in ``package``'s tree, as a manifest-relative POSIX path (``<pkg>/…``).

    A symlink is not a file here. ``is_file()`` follows one, so without the
    guard a symlink standing where a manifest file belongs would be digested
    through to its target and pass; with it, the entry is reported missing. A
    vendored tree holds no symlinks, so one appearing is drift either way.

    The boundary this does NOT close: ``rglob`` does not descend into a
    symlinked directory, so files below one are invisible to the comparison
    whatever this check does. Against the drift this gate is for — a hand-edit,
    a half-finished re-copy — that does not arise, and git records a symlink as
    mode 120000, so it is visible in review rather than silent.
    """
    files: set[str] = set()
    for path in (vendor_dir / package).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(vendor_dir).as_posix()
        if not in_pycache(relative):
            files.add(relative)
    return files


def licence_discrepancies(
    vendor_dir: pathlib.Path, package: str, manifest_name: str, entry: tuple[str, str] | None
) -> list[str]:
    """Any way ``<pkg>.LICENSE`` and the manifest's dist-info licence entry fail to account for each other.

    ``entry`` is that entry as :func:`licence_entry` answered it, or ``None``
    where the manifest carries none. Both are legitimate on their own only in
    one pairing each — an entry with a matching sibling, or neither — and the
    two mismatched pairings are each a discrepancy. A sibling licence with no
    entry to pin it is the one that looks like nothing happening: it is what
    regenerating a wheel's manifest from its own tree leaves behind, and it
    takes the licence assertion away without taking the file away.
    """
    licence_name = f"{package}{LICENSE_SUFFIX}"
    licence_copy = vendor_dir / licence_name
    if entry is None:
        if licence_copy.is_file():
            return [
                f"{licence_name}: pinned by nothing — {manifest_name} carries no dist-info licence entry "
                "to hold it against"
            ]
        return []
    entry_path, entry_digest = entry
    if not licence_copy.is_file():
        return [f"{licence_name}: missing — upstream's licence, copied from {entry_path}"]
    actual = digest_of(licence_copy)
    if actual != entry_digest:
        return [f"{licence_name}: digest {actual} != manifest {entry_digest} ({entry_path})"]
    return []


def tree_discrepancies(vendor_dir: pathlib.Path, package: str, manifest_path: pathlib.Path) -> list[str]:
    """Every way ``package``'s tree differs from what its manifest pins, in stable order.

    A manifest that cannot be opened or decoded is reported like one that does
    not parse: a manifest is a plain-text file we wrote, so either way it pins
    nothing, and either way ending the run over it would cost every other tree
    its report. The clauses are separate only so the message can differ: a
    :class:`ManifestError` is raised already phrased as a finding against its
    manifest, while these two are not, and so are wrapped in the same
    ``<manifest>:`` prefix every other line of the report carries.
    """
    manifest_name = manifest_path.name
    try:
        manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"), manifest_name)
    except ManifestError as error:
        return [str(error)]
    except (OSError, UnicodeDecodeError) as error:
        return [f"{manifest_name}: cannot be read — {error}"]
    prefix = f"{package}/"
    expected = {path: digest for path, digest in manifest.items() if path.startswith(prefix) and not in_pycache(path)}
    if not expected:
        return [f"{manifest_name}: no '{prefix}' entries — it pins no vendored tree"]

    found = vendored_tree_files(vendor_dir, package)
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

    try:
        entry = licence_entry(manifest, manifest_name)
    except ManifestError as error:
        discrepancies.append(str(error))
    else:
        discrepancies.extend(licence_discrepancies(vendor_dir, package, manifest_name, entry))
    return discrepancies


def collect_discrepancies(vendor_dir: pathlib.Path) -> list[str]:
    """Every way ``vendor_dir``'s trees differ from what pins them, grouped by package.

    A manifest naming no tree is walked like any other: its entries all report
    missing, which says more than a bare "the directory is gone" would. A
    manifest that cannot be read — a bad line, undecodable bytes, a file that
    will not open — is one line against its own package and the sweep goes on,
    so one manifest's refusal never costs the report every other tree's drift.
    """
    manifests = manifest_paths(vendor_dir)
    discrepancies: list[str] = []
    for package in sorted(package_dirs(vendor_dir) | manifests.keys()):
        manifest_path = manifests.get(package)
        if manifest_path is None:
            discrepancies.append(
                f"{package}/: no {package}{MANIFEST_SUFFIX} — the tree is pinned by nothing. Vendor the "
                "upstream release manifest beside it, or generate one from the tree if the copy carries a "
                "local patch (py_modules/_vendor/README.md)"
            )
            continue
        discrepancies.extend(tree_discrepancies(vendor_dir, package, manifest_path))
    return discrepancies


def main() -> int:
    discrepancies = collect_discrepancies(VENDOR_DIR)
    if discrepancies:
        print("ERROR: py_modules/_vendor/ is not what its manifests pin:", file=sys.stderr)
        for line in discrepancies:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nA vendored package is an upstream copy — verbatim, or verbatim plus a\n"
            "documented local patch. Never hand-edit one, and never fix a problem here\n"
            "that belongs upstream. Re-copy the tree from the release it pins; where the\n"
            "copy carries a deliberate local patch, reapply the patch to the fresh copy\n"
            "and regenerate its manifest from the result:\n"
            "\n"
            "  cd py_modules/_vendor && find <pkg> -type f -not -path '*/__pycache__/*' \\\n"
            f"      | LC_ALL=C sort | xargs sha256sum > <pkg>{MANIFEST_SUFFIX}\n"
            "\n"
            "Regenerating a manifest is also how this gate is silenced without fixing\n"
            "anything, so it is only ever the last step of such a re-copy, never the answer\n"
            "to a failure above.\n"
            "\n"
            "Per-package provenance and the full update procedure: py_modules/_vendor/README.md",
            file=sys.stderr,
        )
        return 1
    print("OK: every vendored tree matches the manifest that pins it exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
