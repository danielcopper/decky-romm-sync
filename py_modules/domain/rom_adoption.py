"""What the plugin may conclude about ROM content already sitting on disk.

Owns the two judgements adoption rests on: how a collision at the path a
download would write to is described to the user, and whether the bytes on disk
are the ROM the server holds. Pure — the ``stat``, the directory scan and the
hashing all run behind the calling service's adapters and arrive here as values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# RomM publishes CRC32, MD5 and SHA-1 side by side per file and computes them by
# default (``filesystem.skip_hash_calculation`` opts out). MD5 is taken first
# because CRC32 is a 32-bit checksum: at a library's file counts an accidental
# collision is credible, and a match is what authorises the user to keep the
# bytes instead of re-fetching them. SHA-1 is skipped — it costs the same read
# pass as MD5 and buys nothing here, so a second preference tier would only be a
# second thing to keep in sync.
_DIGEST_PREFERENCE: tuple[tuple[str, str], ...] = (("md5", "md5_hash"), ("crc32", "crc_hash"))

_TARGET_OCCUPIED = "target_occupied"


@dataclass(frozen=True)
class ServerFile:
    """One entry of RomM's per-ROM file manifest, reduced to what a check needs.

    *algorithm* and *digest* are empty strings together when the server holds no
    hash for this file — a state the comparison reports as its own outcome
    rather than folding into either a match or a mismatch.

    *rel_path* is where the file belongs **inside the ROM's own directory**, and
    is the empty string when the payload did not state enough to derive one.
    """

    name: str
    size_bytes: int
    algorithm: str
    digest: str
    rel_path: str = ""

    @property
    def verifiable(self) -> bool:
        """Whether the server stated a digest this file's content can be held to."""
        return bool(self.algorithm and self.digest)

    @property
    def lookup_key(self) -> str:
        """The path this entry is matched by: ROM-relative where derivable, else the bare name.

        Falling back to the bare name is weaker — it finds the file wherever it
        sits in the tree — but it is what the plugin can honestly assert when the
        server did not say where the file belongs, and it is the behaviour every
        entry had before ``file_path`` was read.
        """
        return self.rel_path or self.name


@dataclass(frozen=True)
class LocalFile:
    """One observation of a file on disk, as the comparison sees it.

    *digest* is the empty string when it was not computed — either because the
    server had none to compare against or because the sizes already disagreed
    and reading the whole file would have proven nothing new.
    """

    size_bytes: int
    digest: str


@dataclass(frozen=True)
class FileDifference:
    """One named way the content on disk departs from the server's manifest."""

    name: str
    expected: str
    actual: str


def server_manifest(rom_detail: dict[str, Any]) -> tuple[ServerFile, ...]:
    """Reduce RomM's ``files`` list to what a content check needs, per file.

    Entries without a usable ``file_name`` are dropped: they cannot be located on
    disk, so carrying them would turn every comparison into a false "missing".
    An absent or empty ``files`` list yields an empty manifest, which
    :func:`verification_status` reads as "the server cannot confirm this".
    """
    rom_root = _normalize_server_path(rom_detail.get("full_path"))
    manifest: list[ServerFile] = []
    for entry in rom_detail.get("files") or []:
        name = entry.get("file_name") or ""
        if not name:
            continue
        algorithm, digest = _preferred_digest(entry)
        manifest.append(
            ServerFile(
                name=name,
                size_bytes=int(entry.get("file_size_bytes") or 0),
                algorithm=algorithm,
                digest=digest,
                rel_path=_relative_path(rom_root, entry.get("file_path"), name),
            )
        )
    return tuple(manifest)


def _normalize_server_path(raw: object) -> str:
    """Reduce a server-supplied path to ``a/b/c`` — no leading, trailing or empty segments.

    RomM is not guaranteed to hand back a tidy string, and both sides of the
    prefix subtraction below have to be in the same shape for it to mean
    anything.
    """
    if not isinstance(raw, str):
        return ""
    return "/".join(segment for segment in raw.replace("\\", "/").split("/") if segment)


def _relative_path(rom_root: str, raw_file_path: object, name: str) -> str:
    """Where this file belongs inside the ROM's own directory, or ``""``.

    ``RomFile.is_top_level`` in RomM's own model reads
    ``rom.full_path == (file_path if is_nested else full_path)``, so ``file_path``
    and the ROM's ``full_path`` are in one coordinate system: subtracting the
    latter as a prefix yields the file's directory within the ROM, and appending
    ``file_name`` yields its place. That is RomM's comparison, not an inference.

    Returns ``""`` — and the caller then matches on the bare filename — when the
    payload omits ``full_path``, omits ``file_path``, or the two do not actually
    nest. Guessing a prefix from partial data would produce a confident wrong
    location, which is worse than the weaker match it replaces.
    """
    file_dir = _normalize_server_path(raw_file_path)
    if not rom_root or not file_dir:
        return ""
    if file_dir == rom_root:
        return name
    if file_dir.startswith(rom_root + "/"):
        return f"{file_dir[len(rom_root) + 1 :]}/{name}"
    return ""


def _preferred_digest(entry: dict[str, Any]) -> tuple[str, str]:
    """Return ``(algorithm, digest)`` for *entry*, or two empty strings."""
    for algorithm, key in _DIGEST_PREFERENCE:
        digest = (entry.get(key) or "").strip()
        if digest:
            return (algorithm, digest.lower())
    return ("", "")


def sizes_agree(existing_size: int, incoming_size: int) -> bool | None:
    """Whether the bytes on disk and the bytes the server would send match.

    ``None`` when the server stated no size — the comparison cannot be made, and
    reporting it as a mismatch would read as evidence the plugin does not have.
    """
    if not incoming_size:
        return None
    return existing_size == incoming_size


def occupied_target_refusal(
    *,
    path: str,
    is_dir: bool,
    size_bytes: int,
    modified_at: float,
    incoming_name: str,
    incoming_size: int,
    adoptable: bool,
) -> dict[str, Any]:
    """The refusal a download returns when its target path is already taken.

    Carries both sides of the comparison plus the verdict on their sizes, so the
    dialog can state whether they match rather than printing two numbers and
    leaving the subtraction to the user. *adoptable* is false when what is in the
    way is the wrong shape to be this ROM — a folder where the server serves one
    file, or a file where it serves a folder — which leaves replacing or
    cancelling as the only honest exits.
    """
    kind = "folder" if is_dir else "file"
    return {
        "success": False,
        "reason": _TARGET_OCCUPIED,
        "message": f"A {kind} named '{os.path.basename(path)}' is already in place",
        "existing": {
            "name": os.path.basename(path),
            "path": path,
            "is_dir": is_dir,
            "size_bytes": size_bytes,
            "modified_at": modified_at,
        },
        "incoming": {"name": incoming_name, "size_bytes": incoming_size},
        "sizes_match": sizes_agree(size_bytes, incoming_size),
        "adoptable": adoptable,
    }


def compare_manifest(
    manifest: tuple[ServerFile, ...],
    local: dict[str, LocalFile],
) -> tuple[FileDifference, ...]:
    """Name every way *local* departs from *manifest*, most specific first per file.

    *local* is keyed by each entry's :attr:`ServerFile.lookup_key`, so a file the
    server placed in a subdirectory is looked for **there** and a file sitting in
    the wrong one reads as missing. Each difference is reported under that same
    key, which is what tells two same-named files in different subdirectories
    apart.

    Files present on disk but absent from *manifest* are **not** differences: the
    plugin's own multi-file installs carry a generated ``.m3u`` and a healed
    ``PS3_DISC.SFB`` that the server never listed, and a user's dump may carry a
    readme. The check is one-directional — everything the server states must be
    there and must match.
    """
    differences: list[FileDifference] = []
    for entry in manifest:
        key = entry.lookup_key
        found = local.get(key)
        if found is None:
            differences.append(FileDifference(name=key, expected="present", actual="missing"))
            continue
        # A zero server size is "no size stated", so there is nothing to compare
        # — never a size that agrees. Passing this check is therefore not on its
        # own evidence of anything; :func:`verification_status` is what refuses
        # to call such an entry confirmed.
        if entry.size_bytes and found.size_bytes != entry.size_bytes:
            differences.append(
                FileDifference(
                    name=key,
                    expected=f"{entry.size_bytes} bytes",
                    actual=f"{found.size_bytes} bytes",
                )
            )
            continue
        if entry.verifiable and found.digest and found.digest != entry.digest:
            differences.append(
                FileDifference(
                    name=key,
                    expected=f"{entry.algorithm} {entry.digest}",
                    actual=f"{entry.algorithm} {found.digest}",
                )
            )
    return tuple(differences)


def verification_status(
    manifest: tuple[ServerFile, ...],
    local: dict[str, LocalFile],
    differences: tuple[FileDifference, ...],
) -> str:
    """``"match"`` / ``"mismatch"`` / ``"unverifiable"`` for a completed comparison.

    ``"match"`` is the strong claim — *every* file the server put a digest on was
    read and agreed — so it is the one that has to be earned. An absent
    difference is not enough on its own: a file that was never hashed produces no
    difference either, and reading that silence as agreement is how a false
    ``match`` would authorise an adoption whose row carries deletion authority
    (ADR-0028).

    ``"unverifiable"`` therefore covers both ways of having nothing to stand on:
    a server that stated no digest for any file (``filesystem.skip_hash_calculation``,
    or a ROM detail with no file list), and a manifest whose digests were not all
    checked against something. Files the server put *no* digest on are exempt —
    there is nothing to confirm — so a server that hashed only some of its files
    is still verifiable on those.
    """
    verifiable = [entry for entry in manifest if entry.verifiable]
    if not verifiable:
        return "unverifiable"
    if differences:
        return "mismatch"
    # No difference AND nothing skipped. A missing local entry already produced a
    # difference above, so ``.get`` only guards the empty-digest case: an entry
    # the caller chose not to hash.
    unchecked = any(not (found := local.get(entry.lookup_key)) or not found.digest for entry in verifiable)
    return "unverifiable" if unchecked else "match"
