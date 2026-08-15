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

from domain.rom_candidates import DIR, FILE, LINK, Kind

# RomM publishes CRC32, MD5 and SHA-1 side by side — per file and per archive
# member — and computes them by default (``filesystem.skip_hash_calculation``
# opts out). MD5 is taken first because CRC32 is a 32-bit checksum: at a
# library's file counts an accidental collision is credible, and a match is what
# authorises the user to keep the bytes instead of re-fetching them. SHA-1 is
# skipped — it costs the same read pass as MD5 and buys nothing here, so a
# second preference tier would only be a second thing to keep in sync.
_DIGEST_PREFERENCE: tuple[tuple[str, str], ...] = (("md5", "md5_hash"), ("crc32", "crc_hash"))

_TARGET_OCCUPIED = "target_occupied"

# How each kind is named in the one-line message. The kindless case is deliberately
# vague: the plugin has looked and has no word for what is there, and guessing one
# would be the same invention that let a named pipe be offered as a game.
_A_KIND: dict[Kind | None, str] = {
    FILE: "A file",
    DIR: "A folder",
    LINK: "A shortcut",
    None: "Something",
}

# The extensions RomM reads as archives, so its digest for such a file describes
# the content inside rather than the container's own bytes (``ARCHIVE_READERS``
# in its ``roms_handler``, plus the plain compressors its older whole-file
# hasher decompressed). Matched on the name because that is the only thing the
# plugin knows before opening anything — and the name at the target path is the
# server's own.
_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".gz",
    ".bz2",
    ".xz",
)

_CONTENTS_DIFFER = "contents differ from the server's copy"


def is_archive_name(name: str) -> bool:
    """Whether *name* is one RomM would have hashed by its contents, not its bytes."""
    return name.lower().endswith(_ARCHIVE_SUFFIXES)


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

    For a file RomM read as an archive the digest describes the **content**
    inside it and never the container's own bytes, while ``file_size_bytes`` —
    and therefore *size_bytes* — is the container's size on disk. *crc32* is
    carried beside the preferred digest because a ZIP's central directory states
    the same number per member for free.

    *members* is non-empty only when the payload carried ``archive_members``,
    which is an optional extra rather than the carrier: a server that has not
    rescanned its library since RomM 4.9.0 states none, and its file-level
    digest still describes the content. Where they are there they are better
    evidence, because they name every member separately.

    *rel_path* is where the file belongs **inside the ROM's own directory**, and
    is the empty string when the payload did not state enough to derive one.
    """

    name: str
    size_bytes: int
    algorithm: str
    digest: str
    rel_path: str = ""
    members: tuple[ServerMember, ...] = ()
    crc32: str = ""

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

    *is_archive* says the name is one RomM would have hashed by its contents, so
    the container's own bytes answer to nothing the server published. *members*
    is what was found inside it, and ``None`` when nothing was looked for (an
    ordinary file) or when the container could not be opened. An empty tuple
    means the opposite — it was opened and holds nothing.
    """

    size_bytes: int
    digest: str
    members: tuple[LocalMember, ...] | None = None
    is_archive: bool = False


@dataclass(frozen=True)
class FileDifference:
    """One named way the content on disk departs from the server's manifest.

    *detail* is the whole sentence the user is shown after the name, so the
    finding reads as one line. Two digests said no more than "these differ" and
    wrapped the line into a block nobody could read; a size difference states
    both numbers, because those are numbers a person can act on.
    """

    name: str
    detail: str


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
                crc32=(entry.get("crc_hash") or "").strip().lower(),
            )
        )
    return tuple(manifest)


def _archive_members(entry: dict[str, Any]) -> tuple[ServerMember, ...]:
    """Reduce one file entry's ``archive_members`` to what a member check needs.

    Absent whenever RomM did not read the file as an archive — and absent too
    for archives it did, on every library it has not rescanned since 4.9.0
    stored them, which is why they cannot be the only path. Members without a
    name are dropped for the same reason a nameless file is: they cannot be
    found inside the archive, so they could only read as missing.
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


def adoptable_content(kind: Kind | None, *, served_dir: bool) -> bool:
    """Whether content of this *kind* could become this ROM's install row.

    The one expression behind every adoption decision — the gate's ``adoptable``
    flag, the validation immediately before the move, and the last check after
    it. Written as a single equality rather than a chain of refusals because the
    positive set is what is short: a directory where the server serves a folder,
    a file where it serves one, and nothing else. A link fails it whatever it
    resolves to (an install row has to be removable and the uninstall path
    refuses one), and so does content with no kind at all.
    """
    return kind == (DIR if served_dir else FILE)


def unadoptable_reason(kind: Kind | None) -> str:
    """Why content of this *kind* cannot be this ROM, in one sentence.

    Only called where :func:`adoptable_content` said no, which is what makes the
    served shape unnecessary here: a directory that was refused was refused
    because the server serves one file, and a file because it serves a folder.
    Taking the shape as a parameter would offer a second place for the two to
    disagree about the same refusal.
    """
    if kind == LINK:
        return "A shortcut is in the way — a shortcut cannot be used as this game"
    if kind is None:
        return "What is in the way is neither a file nor a folder"
    return (
        "A folder is in the way where a file belongs" if kind == DIR else "A file is in the way where a folder belongs"
    )


def occupied_target_refusal(
    *,
    path: str,
    kind: Kind | None,
    size_bytes: int,
    modified_at: float,
    incoming_name: str,
    incoming_size: int,
    served_dir: bool,
) -> dict[str, Any]:
    """The refusal a download returns when its target path is already taken.

    Carries both sides of the comparison plus the verdict on their sizes, so the
    dialog can state whether they match rather than printing two numbers and
    leaving the subtraction to the user. ``adoptable`` is derived here rather
    than passed in: it is :func:`adoptable_content`, and a caller that computed
    it separately is a second copy of the rule.

    The size verdict is withheld for anything but a file or a folder. A link's
    ``stat`` reports the length of the path it stores, not of the content behind
    it, so relating that number to the server's would be a comparison of two
    unrelated things dressed as evidence.
    """
    return {
        "success": False,
        "reason": _TARGET_OCCUPIED,
        "message": f"{_A_KIND[kind]} named '{os.path.basename(path)}' is already in place",
        "existing": {
            "name": os.path.basename(path),
            "path": path,
            "kind": kind,
            "size_bytes": size_bytes,
            "modified_at": modified_at,
        },
        "incoming": {"name": incoming_name, "size_bytes": incoming_size},
        "sizes_match": sizes_agree(size_bytes, incoming_size) if kind in (FILE, DIR) else None,
        "adoptable": adoptable_content(kind, served_dir=served_dir),
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
            differences.append(FileDifference(name=key, detail="missing"))
        elif found.members is not None and entry.archived:
            differences.extend(_stated_member_differences(entry, found))
        else:
            difference = _single_difference(entry, found)
            if difference is not None:
                differences.append(difference)
    return tuple(differences)


def _single_difference(entry: ServerFile, found: LocalFile) -> FileDifference | None:
    """The one way this entry can depart from the manifest, or ``None``.

    Everything but a set of stated archive members has at most one finding, and
    which comparison is available depends on what is on disk. An archive that
    could not be opened falls through to ``None`` on purpose: the server's digest
    speaks for content this plugin cannot produce, so there is nothing to compare
    and nothing to allege.
    """
    if found.members is not None:
        return _sole_member_difference(entry, found)
    if entry.archived:
        return _unpacked_difference(entry, found)
    if found.is_archive:
        return None
    return _file_difference(entry, found)


def _file_difference(entry: ServerFile, found: LocalFile) -> FileDifference | None:
    """Hold one unarchived entry to the bytes of the file on disk."""
    # A zero server size is "no size stated", so there is nothing to compare —
    # never a size that agrees. Passing this check is therefore not on its own
    # evidence of anything; :func:`verification_status` is what refuses to call
    # such an entry confirmed.
    if entry.size_bytes and found.size_bytes != entry.size_bytes:
        return FileDifference(entry.lookup_key, f"expected {entry.size_bytes} bytes, found {found.size_bytes}")
    if entry.verifiable and found.digest and found.digest != entry.digest:
        return FileDifference(entry.lookup_key, _CONTENTS_DIFFER)
    return None


def _stated_member_differences(entry: ServerFile, found: LocalFile) -> tuple[FileDifference, ...]:
    """Hold each member the server named to the member of that name on disk.

    The one comparison that can report several findings at once — a set of
    members can be wrong in as many ways as it has members. Members on disk the
    server did not name are allowed: RomM's scanner drops excluded names and
    extensions from ``archive_members``, so even a byte-identical copy of the
    server's own archive can hold more than it listed.

    The container's own size is deliberately not compared here or below: it
    describes the packing, and two archives of the same ROM differ in it
    whenever they were packed differently.
    """
    on_disk = {member.name: member for member in found.members or ()}
    differences: list[FileDifference] = []
    for member in entry.members:
        name = _member_key(entry, member.name)
        local_member = on_disk.get(member.name)
        if local_member is None:
            differences.append(FileDifference(name=name, detail="missing from the archive"))
        elif member.size_bytes and local_member.size_bytes != member.size_bytes:
            differences.append(
                FileDifference(name, f"expected {member.size_bytes} bytes, found {local_member.size_bytes}")
            )
        elif _digests_disagree(member.crc32, local_member.crc32) or _digests_disagree(
            member.digest, local_member.digest
        ):
            differences.append(FileDifference(name, _CONTENTS_DIFFER))
    return tuple(differences)


def _sole_member_difference(entry: ServerFile, found: LocalFile) -> FileDifference | None:
    """Hold an archive of exactly one member to the digest the server published for it.

    That digest is the archive's content identity under every rule RomM has
    hashed archives with: the composite over one member reduces to that member,
    and the older whole-file hasher took the largest member — which, with one
    member, is the same bytes. With **several** members the two rules disagree
    and the payload does not say which produced the number, so nothing is
    compared and :func:`verification_status` reports that it cannot be
    confirmed.
    """
    members = found.members
    if members is None or len(members) != 1 or not entry.verifiable:
        return None
    member = members[0]
    if _digests_disagree(entry.crc32, member.crc32) or _digests_disagree(entry.digest, member.digest):
        return FileDifference(entry.lookup_key, _CONTENTS_DIFFER)
    return None


def _digests_disagree(stated: str, observed: str) -> bool:
    """Whether two digests contradict each other, as opposed to one being absent."""
    return bool(stated and observed and stated != observed)


def _unpacked_difference(entry: ServerFile, found: LocalFile) -> FileDifference | None:
    """Hold a file that is not an archive to the one member it could be."""
    member = unpacked_member(entry, found.size_bytes)
    if member is None or not (member.verifiable and found.digest) or found.digest == member.digest:
        return None
    return FileDifference(name=_member_key(entry, member.name), detail=_CONTENTS_DIFFER)


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


def digests_to_read(entry: ServerFile, found: LocalFile) -> tuple[DigestRequest, ...]:
    """Decide which bytes must be read to hold *entry* to what the server stated.

    *found* is the observation so far — its size, whether its name says archive,
    and the members the central directory listed — with no digest computed yet.

    Cheap evidence first, in the order it costs. A size that already disagrees —
    or, inside an archive, a CRC32 the central directory hands over for free —
    is proof of a difference, and re-reading a gigabyte to restate it would cost
    the user tens of seconds at the read rate of a memory card. Cheap
    *agreement* is never the answer, only the reason to go and read: CRC32 is a
    32-bit checksum, at a library's member counts an accidental collision is
    credible, and a ``match`` is what authorises keeping bytes the server would
    otherwise re-send — so what earns the strong claim is the digest RomM
    published beside it, over the decompressed content.
    """
    if found.members is not None and entry.archived:
        return _stated_member_digests_to_read(entry, found.members)
    request = _single_digest_to_read(entry, found)
    planned: list[DigestRequest] = [] if request is None else [request]
    return tuple(planned)


def _single_digest_to_read(entry: ServerFile, found: LocalFile) -> DigestRequest | None:
    """The one read that can confirm this entry, or ``None`` when none can.

    A file the server described as a whole is answered by one digest whatever it
    holds: the sole member of an archive it stated nothing about, the loose bytes
    of a member someone unpacked, or the file itself. An archive that could not
    be opened, and a file with nothing stated to hold it to, are answered by
    neither — reading the container's bytes would answer a question the server
    never asked.
    """
    if found.members is not None:
        return _sole_member_digest_to_read(entry, found.members)
    if entry.archived:
        member = unpacked_member(entry, found.size_bytes)
        if member is None or not member.verifiable:
            return None
        return DigestRequest(member="", algorithm=member.algorithm, size_bytes=found.size_bytes)
    if found.is_archive or not entry.verifiable:
        return None
    if entry.size_bytes and found.size_bytes != entry.size_bytes:
        return None
    return DigestRequest(member="", algorithm=entry.algorithm, size_bytes=found.size_bytes)


def _sole_member_digest_to_read(entry: ServerFile, local_members: tuple[LocalMember, ...]) -> DigestRequest | None:
    """The member to decompress when the server described the archive as a whole."""
    if len(local_members) != 1 or not entry.verifiable:
        return None
    member = local_members[0]
    if _digests_disagree(entry.crc32, member.crc32):
        return None
    return DigestRequest(member=member.name, algorithm=entry.algorithm, size_bytes=member.size_bytes)


def _stated_member_digests_to_read(
    entry: ServerFile, local_members: tuple[LocalMember, ...]
) -> tuple[DigestRequest, ...]:
    """Which of the members the server named are worth decompressing."""
    on_disk = {found.name: found for found in local_members}
    requests: list[DigestRequest] = []
    for member in entry.members:
        found = on_disk.get(member.name)
        if found is None or not member.verifiable:
            continue
        if member.size_bytes and found.size_bytes != member.size_bytes:
            continue
        if _digests_disagree(member.crc32, found.crc32):
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
    is still verifiable on those. An archive is confirmed by what is inside it,
    so a partial one cannot reach ``"match"``: a member the server stated and the
    container does not hold is a difference, and one that was never read leaves
    the entry unchecked. An archive of several members the server described only
    as a whole is unchecked by construction — see
    :func:`_sole_member_differences` for why nothing can be concluded there.
    """
    verifiable = [entry for entry in manifest if entry.verifiable]
    if not verifiable:
        return "unverifiable"
    if differences:
        return "mismatch"
    return "match" if all(_entry_confirmed(entry, local) for entry in verifiable) else "unverifiable"


def unconfirmed_reason(manifest: tuple[ServerFile, ...], local: dict[str, LocalFile]) -> str:
    """Why a comparison that found no difference still cannot confirm anything.

    ``"whole_archive"`` when what stopped it was an archive the server published
    one digest for and this plugin cannot attribute to any single thing inside
    it — nothing failed, the number simply cannot be interpreted. ``"unread"``
    for every other way of coming up short, all of which are a read that did not
    happen. The first unconfirmed entry decides, which is the only entry there is
    for a single-file ROM.
    """
    for entry in manifest:
        if not entry.verifiable or _entry_confirmed(entry, local):
            continue
        found = local.get(entry.lookup_key)
        if found is not None and not entry.archived and found.members is not None and len(found.members) != 1:
            return "whole_archive"
        return "unread"
    return "unread"


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
    if found.members is None:
        if entry.archived:
            member = unpacked_member(entry, found.size_bytes)
            return member is not None and member.verifiable and bool(found.digest)
        return not found.is_archive and bool(found.digest)
    if not entry.archived:
        return len(found.members) == 1 and bool(found.members[0].digest)
    on_disk = {member.name: member for member in found.members}
    return all(
        (local_member := on_disk.get(member.name)) is not None and bool(local_member.digest)
        for member in entry.members
        if member.verifiable
    )
