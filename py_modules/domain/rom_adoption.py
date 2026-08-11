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
    """

    name: str
    size_bytes: int
    algorithm: str
    digest: str

    @property
    def verifiable(self) -> bool:
        """Whether the server stated a digest this file's content can be held to."""
        return bool(self.algorithm and self.digest)


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
    """Reduce RomM's ``files`` list to the (name, size, digest) triples a check needs.

    Entries without a usable ``file_name`` are dropped: they cannot be located on
    disk, so carrying them would turn every comparison into a false "missing".
    An absent or empty ``files`` list yields an empty manifest, which
    :func:`verification_status` reads as "the server cannot confirm this".
    """
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
            )
        )
    return tuple(manifest)


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

    Files present on disk but absent from *manifest* are **not** differences: the
    plugin's own multi-file installs carry a generated ``.m3u`` and a healed
    ``PS3_DISC.SFB`` that the server never listed, and a user's dump may carry a
    readme. The check is one-directional — everything the server states must be
    there and must match.
    """
    differences: list[FileDifference] = []
    for entry in manifest:
        found = local.get(entry.name)
        if found is None:
            differences.append(FileDifference(name=entry.name, expected="present", actual="missing"))
            continue
        if entry.size_bytes and found.size_bytes != entry.size_bytes:
            differences.append(
                FileDifference(
                    name=entry.name,
                    expected=f"{entry.size_bytes} bytes",
                    actual=f"{found.size_bytes} bytes",
                )
            )
            continue
        if entry.verifiable and found.digest and found.digest != entry.digest:
            differences.append(
                FileDifference(
                    name=entry.name,
                    expected=f"{entry.algorithm} {entry.digest}",
                    actual=f"{entry.algorithm} {found.digest}",
                )
            )
    return tuple(differences)


def verification_status(manifest: tuple[ServerFile, ...], differences: tuple[FileDifference, ...]) -> str:
    """``"match"`` / ``"mismatch"`` / ``"unverifiable"`` for a completed comparison.

    ``"unverifiable"`` is reserved for a server that stated no digest for any
    file — a RomM with ``filesystem.skip_hash_calculation`` set, or a ROM whose
    detail carries no file list at all. It is deliberately neither of the other
    two: sizes alone say the content is plausible, never that it is the same.
    A server that hashed *some* of the files is verifiable on those.
    """
    if not any(entry.verifiable for entry in manifest):
        return "unverifiable"
    return "mismatch" if differences else "match"
