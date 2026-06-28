"""Combine per-entry hashes of a multi-file (zip) save into one content hash.

RomM stores a multi-file save as a single zipped asset and identifies it by a
content hash computed *per zip entry*, not over the raw archive bytes — the zip
container's own framing (member order, timestamps, compression) is not stable,
so hashing the archive whole would never converge across clients. The plugin
must reproduce that exact scheme so a zip save's local and server hashes match
and save-sync converges instead of round-tripping forever.

This is the pure half of that computation: given the already-hashed zip entries,
derive the single combined hash. Reading the archive and hashing each entry's
bytes is I/O and lives in the ``SaveFileStore`` adapter. No I/O, no
service/adapter/lib imports — stdlib only.
"""

from __future__ import annotations

import hashlib


def combine_zip_entry_hashes(entries: list[tuple[str, str]]) -> str:
    """Combine ``(entry_name, entry_md5_hex)`` pairs into RomM's zip content hash.

    Mirrors RomM's ``_compute_zip_hash`` byte-for-byte: the entries are sorted
    by name, rendered one per line as ``name:hexdigest``, joined with ``\\n``,
    and the UTF-8 encoding of that block is MD5-hashed. Sorting by name makes the
    result independent of the order entries were read from the archive; an empty
    list (an archive with no file entries) hashes the empty string.

    Each ``entry_md5_hex`` is the MD5 of that entry's raw bytes, computed by the
    caller (the adapter, which owns the archive I/O) — it is the ``name``/hash
    pairing that RomM hashes, not the entry bytes themselves.
    """
    lines = [f"{name}:{digest}" for name, digest in sorted(entries, key=lambda entry: entry[0])]
    combined = "\n".join(lines)
    return hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()
