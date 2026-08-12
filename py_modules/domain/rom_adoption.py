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

# RomM publishes CRC32, MD5 and SHA-1 side by side — per file and per archive
# member — and computes them by default (``filesystem.skip_hash_calculation``
# opts out). MD5 is taken first
# because CRC32 is a 32-bit checksum: at a library's file counts an accidental
# collision is credible, and a match is what authorises the user to keep the
# bytes instead of re-fetching them. SHA-1 is skipped — it costs the same read
# pass as MD5 and buys nothing here, so a second preference tier would only be a
# second thing to keep in sync.
_DIGEST_PREFERENCE: tuple[tuple[str, str], ...] = (("md5", "md5_hash"), ("crc32", "crc_hash"))

_TARGET_OCCUPIED = "target_occupied"


@dataclass(frozen=True)
class ServerMember:
    """One file inside an archived ROM, as RomM's ``archive_members`` states it.

    *size_bytes* is the member's **uncompressed** size and the digests are taken
    over its decompressed bytes, so both have a counterpart a ZIP's central
    directory states for free. *crc32* is carried beside the preferred digest
    rather than instead of it: a CRC32 disagreement is free proof of a
    difference, while agreement is too weak to be the evidence a ``match``
    rests on.
    """

    name: str
    size_bytes: int
    algorithm: str
    digest: str
    crc32: str = ""

    @property
    def verifiable(self) -> bool:
        """Whether the server stated a digest this member's content can be held to."""
        return bool(self.algorithm and self.digest)


@dataclass(frozen=True)
class ServerFile:
    """One entry of RomM's per-ROM file manifest, reduced to what a check needs.

    *algorithm* and *digest* are empty strings together when the server holds no
    hash for this file — a state the comparison reports as its own outcome
    rather than folding into either a match or a mismatch.

    *members* is non-empty when RomM stated what is **inside** this file. Its
    own *algorithm* / *digest* are then not comparable with anything on disk:
    RomM hashes an archive's decompressed members in ASCII name order into one
    accumulator, so the file-level digest is a composite over the content, never
    the container's own bytes, while ``file_size_bytes`` — and therefore
    *size_bytes* — is the container's size on disk. The members carry the
    content identity, so they are what an archived entry is held to.

    *rel_path* is where the file belongs **inside the ROM's own directory**, and
    is the empty string when the payload did not state enough to derive one.
    """

    name: str
    size_bytes: int
    algorithm: str
    digest: str
    rel_path: str = ""
    members: tuple[ServerMember, ...] = ()

    @property
    def archived(self) -> bool:
        """Whether the server stated what sits inside this file."""
        return bool(self.members)

    @property
    def verifiable(self) -> bool:
        """Whether the server stated a digest this file's content can be held to."""
        if self.members:
            return any(member.verifiable for member in self.members)
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
class LocalMember:
    """One member of an archive on disk, as its central directory states it.

    *size_bytes* and *crc32* come from the directory at no cost; *digest* is the
    empty string until the member is decompressed, and stays empty when it was
    not worth reading or could not be read at all.
    """

    name: str
    size_bytes: int
    crc32: str
    digest: str = ""


@dataclass(frozen=True)
class LocalFile:
    """One observation of a file on disk, as the comparison sees it.

    *digest* is the empty string when it was not computed — either because the
    server had none to compare against or because the sizes already disagreed
    and reading the whole file would have proven nothing new.

    *members* is ``None`` when the file was not read as an archive, which covers
    both the ordinary case (the server stated no members, so there was nothing
    to look inside for) and the one where it did and the container could not be
    opened. An empty tuple means the opposite — it was opened and holds nothing.
    """

    size_bytes: int
    digest: str
    members: tuple[LocalMember, ...] | None = None


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
                members=_archive_members(entry),
            )
        )
    return tuple(manifest)


def _archive_members(entry: dict[str, Any]) -> tuple[ServerMember, ...]:
    """Reduce one file entry's ``archive_members`` to what a member check needs.

    Absent for every file RomM did not read as an archive, and absent too for an
    archive it could not read — an empty or damaged one, or one whose every
    member its scanner excludes — where the file-level digest **is** the
    container's own bytes and the whole-file comparison is the correct one.
    Members without a name are dropped for the same reason a nameless file is:
    they cannot be found inside the archive, so they could only read as missing.
    """
    members: list[ServerMember] = []
    for raw in entry.get("archive_members") or []:
        name = raw.get("name") or ""
        if not name:
            continue
        algorithm, digest = _preferred_digest(raw)
        members.append(
            ServerMember(
                name=name,
                size_bytes=int(raw.get("size") or 0),
                algorithm=algorithm,
                digest=digest,
                crc32=(raw.get("crc_hash") or "").strip().lower(),
            )
        )
    return tuple(members)


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
        elif entry.archived:
            differences.extend(_member_differences(entry, found))
        else:
            differences.extend(_file_differences(entry, found))
    return tuple(differences)


def _file_differences(entry: ServerFile, found: LocalFile) -> tuple[FileDifference, ...]:
    """Hold one unarchived entry to the bytes of the file on disk."""
    # A zero server size is "no size stated", so there is nothing to compare —
    # never a size that agrees. Passing this check is therefore not on its own
    # evidence of anything; :func:`verification_status` is what refuses to call
    # such an entry confirmed.
    if entry.size_bytes and found.size_bytes != entry.size_bytes:
        return (FileDifference(entry.lookup_key, f"{entry.size_bytes} bytes", f"{found.size_bytes} bytes"),)
    if entry.verifiable and found.digest and found.digest != entry.digest:
        return (
            FileDifference(
                name=entry.lookup_key,
                expected=f"{entry.algorithm} {entry.digest}",
                actual=f"{entry.algorithm} {found.digest}",
            ),
        )
    return ()


def _member_differences(entry: ServerFile, found: LocalFile) -> tuple[FileDifference, ...]:
    """Hold one archived entry to its members, never to the container's bytes.

    The archive's own size is deliberately not compared: it describes the
    container, and two archives of the same ROM differ in it whenever they were
    packed differently. Every member the server stated must be present and must
    agree; members on disk the server did not state are allowed, because RomM's
    scanner drops excluded names and extensions from ``archive_members``, so
    even a byte-identical copy of the server's own archive can hold more than it
    listed.
    """
    if found.members is None:
        return _unpacked_differences(entry, found)
    on_disk = {member.name: member for member in found.members}
    differences: list[FileDifference] = []
    for member in entry.members:
        name = _member_key(entry, member.name)
        local_member = on_disk.get(member.name)
        if local_member is None:
            differences.append(FileDifference(name=name, expected="present", actual="missing"))
        elif member.size_bytes and local_member.size_bytes != member.size_bytes:
            differences.append(FileDifference(name, f"{member.size_bytes} bytes", f"{local_member.size_bytes} bytes"))
        elif member.crc32 and local_member.crc32 and local_member.crc32 != member.crc32:
            differences.append(FileDifference(name, f"crc32 {member.crc32}", f"crc32 {local_member.crc32}"))
        elif member.verifiable and local_member.digest and local_member.digest != member.digest:
            differences.append(
                FileDifference(name, f"{member.algorithm} {member.digest}", f"{member.algorithm} {local_member.digest}")
            )
    return tuple(differences)


def _unpacked_differences(entry: ServerFile, found: LocalFile) -> tuple[FileDifference, ...]:
    """Hold a file that is not an archive to the one member it could be."""
    member = unpacked_member(entry, found.size_bytes)
    if member is None or not (member.verifiable and found.digest) or found.digest == member.digest:
        return ()
    return (
        FileDifference(
            name=_member_key(entry, member.name),
            expected=f"{member.algorithm} {member.digest}",
            actual=f"{member.algorithm} {found.digest}",
        ),
    )


def _member_key(entry: ServerFile, member_name: str) -> str:
    """Name one member the way the user is shown it: its place inside the archive."""
    return f"{entry.lookup_key}/{member_name}"


@dataclass(frozen=True)
class DigestRequest:
    """One digest the comparison has decided is worth reading the bytes for.

    *member* is the empty string for the file's own bytes and a member's name
    inside the archive otherwise. *size_bytes* is what that read will cost, so a
    caller can total the work before starting it.
    """

    member: str
    algorithm: str
    size_bytes: int


def digests_to_read(
    entry: ServerFile,
    local_size: int,
    local_members: tuple[LocalMember, ...] | None,
) -> tuple[DigestRequest, ...]:
    """Decide which bytes must be read to hold *entry* to what the server stated.

    Cheap evidence first, in the order it costs. A size that already disagrees —
    or, inside an archive, a CRC32 the central directory hands over for free —
    is proof of a difference, and re-reading a gigabyte to restate it would cost
    the user 20 seconds (measured: 77 MiB/s on a Steam Deck SD card). Cheap
    *agreement* is never the answer, only the reason to go and read: CRC32 is a
    32-bit checksum, at a library's member counts an accidental collision is
    credible, and a ``match`` is what authorises keeping bytes the server would
    otherwise re-send — so what earns the strong claim is the digest RomM
    published beside it, over the member's decompressed content.
    """
    if not entry.archived:
        if not entry.verifiable or (entry.size_bytes and local_size != entry.size_bytes):
            return ()
        return (DigestRequest(member="", algorithm=entry.algorithm, size_bytes=local_size),)
    if local_members is None:
        member = unpacked_member(entry, local_size)
        if member is None or not member.verifiable:
            return ()
        return (DigestRequest(member="", algorithm=member.algorithm, size_bytes=local_size),)
    on_disk = {found.name: found for found in local_members}
    requests: list[DigestRequest] = []
    for member in entry.members:
        found = on_disk.get(member.name)
        if found is None or not member.verifiable:
            continue
        if member.size_bytes and found.size_bytes != member.size_bytes:
            continue
        if member.crc32 and found.crc32 and found.crc32 != member.crc32:
            continue
        requests.append(DigestRequest(member=member.name, algorithm=member.algorithm, size_bytes=found.size_bytes))
    return tuple(requests)


def unpacked_member(entry: ServerFile, local_size: int) -> ServerMember | None:
    """The member a file that is not an archive may be held to, if any.

    A user who unpacked what the server keeps packed leaves one loose file where
    the archive would be. It can only be compared against a **single**-member
    archive — a composite over several members has no counterpart in one file —
    and only when the sizes agree, which is what tells an unpacked member apart
    from an archive format this plugin cannot open. Comparing those bytes to a
    member's digest anyway would report a mismatch on content that is correct,
    which is the failure this whole comparison exists to avoid.
    """
    if len(entry.members) != 1:
        return None
    member = entry.members[0]
    return member if member.size_bytes and local_size == member.size_bytes else None


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
    is still verifiable on those. An archived entry is confirmed by its members,
    so a partial archive cannot reach ``"match"``: a member the server stated and
    the container does not hold is a difference, and one that was never read
    leaves the entry unchecked.
    """
    verifiable = [entry for entry in manifest if entry.verifiable]
    if not verifiable:
        return "unverifiable"
    if differences:
        return "mismatch"
    return "match" if all(_entry_confirmed(entry, local) for entry in verifiable) else "unverifiable"


def _entry_confirmed(entry: ServerFile, local: dict[str, LocalFile]) -> bool:
    """Whether everything the server put a digest on for *entry* was read and agreed.

    Reached only once no difference was found, so this asks the remaining
    question: was the agreement observed, or merely not contradicted? A missing
    local entry already produced a difference, so the ``None`` guard covers the
    case where the caller located nothing to hash.
    """
    found = local.get(entry.lookup_key)
    if found is None:
        return False
    if not entry.archived:
        return bool(found.digest)
    if found.members is None:
        member = unpacked_member(entry, found.size_bytes)
        return member is not None and member.verifiable and bool(found.digest)
    on_disk = {member.name: member for member in found.members}
    return all(
        (local_member := on_disk.get(member.name)) is not None and bool(local_member.digest)
        for member in entry.members
        if member.verifiable
    )
