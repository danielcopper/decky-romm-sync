"""RetroArch ``.info`` files — the format, and the firmware a core declares.

RetroArch ships a ``<core>.info`` file next to every ``<core>.so`` in its cores
directory. It looks like an INI file, and it is one in the most literal sense:
there is no second parser for it. ``core_info_get_config_file`` hands the path
to ``config_file_new_from_path_to_string`` (``core_info.c:1631-1642``), which
reads the file and walks it through the very ``config_file_parse_line``
pipeline ``retroarch.cfg`` goes through (``config_file.c:829-852``, ``:634-688``).
The grammar is therefore the one ported in :mod:`atlas.retroarch_cfg`, down to
the first-of-a-repeated-key rule and the NUL cut.

What *is* specific to ``.info`` is the firmware enumeration: which keys a core
info read asks for, and how many. That is :func:`enumerate_firmware`, a port of
``core_info_resolve_firmware``. Both halves are pure text in, value objects out
— the caller decides how to interpret a field and never touches the filesystem
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .retroarch_cfg import cfg_bool, cfg_uint, parse_cfg_text


def parse_core_info(text: str) -> dict[str, str]:
    """Parse a ``.info`` file's content into a key-value dict.

    This is :func:`atlas.retroarch_cfg.parse_cfg_text` under the name the
    domain uses, because RetroArch reads both file kinds with one parser (see
    the module docstring). What follows from that, and is not what an INI
    reader would do: a key is a run of graph characters that must be followed
    by ``=``, so ``corename="mGBA"`` sets nothing; a quoted value ends at the
    **next** quote, not at the last one on the line; a ``#`` outside a string
    literal starts a comment; the **first** of a repeated key wins; and nothing
    past a NUL byte is read at all.
    """
    return parse_cfg_text(text)


FIRMWARE_COUNT = "firmware_count"

_FIRMWARE = "firmware"
_PATH = "path"
_OPT = "opt"
_DESC = "desc"

# ``char prefix[12]`` holds "firmware" plus what snprintf writes into the four
# bytes behind it (``core_info.c:1577``, ``:1590-1599``).
_PREFIX_ROOM = 3

# Why an enumeration passed over a ``firmware…path`` key the file states. The
# three are the three places a declaration can fall out of
# ``core_info_resolve_firmware`` (``core_info.c:1572-1629``), and they are told
# apart because each is a different mistake in the file: a key nobody composes,
# a slot nobody reaches, and a slot read and then thrown away.
UNREAD_NO_SLOT = "no-slot"
UNREAD_UNCOUNTED = "uncounted"
UNREAD_EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class FirmwareSlot:
    """One firmware slot of a core, as ``core_info_resolve_firmware`` fills it.

    A slot is an array element, not a line in the file: RetroArch allocates
    ``firmware_count`` of them and then looks up each one's three keys.
    ``path`` is always non-empty here — a slot whose ``firmwareN_path`` is
    absent or empty keeps the NULL its ``calloc`` gave it and is skipped by
    every read that follows (``core_info.c:1610-1614``, ``:2378``), so it
    declares nothing and :func:`enumerate_firmware` does not return one. A slot
    the file *states* and empties that way is not nothing to a reader of the
    file, so it comes back under ``unread`` instead of coming back at all.
    """

    index: int
    path: str
    description: str
    optional: bool


@dataclass(frozen=True, slots=True)
class FirmwareEnumeration:
    """The firmware one ``.info`` declares, bounded by its own ``firmware_count``.

    ``count`` is that field verbatim, as the file spells it — empty when the
    file states none. ``unread`` names every ``firmware…path`` key the file
    states and this enumeration did not take, whether because the composer
    never asks for that spelling, because the count does not reach the slot, or
    because the value stated there is empty (:func:`unread_reason` tells them
    apart). Together with ``slots`` it accounts for every path key in the file:
    a declaration is read or it is stated, never dropped. That gap between what
    the file says and what the emulator does is what nothing else in a firmware
    answer would show.

    ``unread_stating_a_path`` is the part of ``unread`` that put a value behind
    the key. The two are not the same question: an unread key is a line the
    file spends and the emulator ignores, which is worth saying whatever it
    holds, while only one with a value could have been *about* some particular
    file. This is the only place both are known — the values do not leave the
    enumeration — so a caller asking the second question has to be handed the
    answer here. A value that states a path naming no file (``"pcsx2/"``) is
    still a stated path; whether a path names a file is a separate question
    with its own answer.
    """

    slots: tuple[FirmwareSlot, ...]
    count: str
    unread: tuple[str, ...]
    unread_stating_a_path: tuple[str, ...]


def firmware_key(index: int, field: str) -> str:
    """The key a core info read looks up for one slot — the exact spelling.

    ``core_info_resolve_firmware`` *composes* this key, it never parses one:
    ``"firmware"``, then ``snprintf(prefix + 8, 4, "%u_", i)``, then the field
    name (``core_info.c:1590-1616``). Two consequences worth naming. Nothing
    but this spelling is ever looked up, so ``firmware00_path`` and a
    ``firmware٠_path`` written with non-ASCII digits are keys no read asks for.
    And the four bytes behind the prefix are part of the spelling: from index
    100 on the ``_`` no longer fits, and RetroArch asks for
    ``firmware100path``.
    """
    return f"{_FIRMWARE}{f'{index}_'[:_PREFIX_ROOM]}{field}"


def _states_a_path(key: str) -> bool:
    """Does *key* state a firmware path at all — however badly it is spelled?

    The widest reading that is still about firmware paths, and deliberately so:
    a key that opens with ``firmware`` and closes with ``path`` is a line a
    reader of the file takes for a firmware declaration, which is the whole
    reason it must not vanish when RetroArch passes over it. That takes in
    every spelling :func:`firmware_key` composes and every one it does not
    (``firmware_path``, ``firmwareA_path``, ``firmware00_path``).

    It leaves out the keys that name something other than a file. One shipped
    ``.info`` states ``firmware0_md5``, a key RetroArch reads nowhere — but a
    checksum is not a declared path, and reporting it as one would put a file
    nobody declared into an answer about declared files.
    """
    return key.startswith(_FIRMWARE) and key.endswith(_PATH)


def _composed_index(key: str) -> int | None:
    """The slot whose composed ``path`` key is exactly *key* — ``None`` when none is.

    The check runs the composer forwards: whatever the key looks like, it
    belongs to a slot only when it is *exactly* what :func:`firmware_key` would
    have asked for at that index. So ``firmware00_path`` belongs to no slot,
    ``firmware100path`` is slot 100 — and ``firmware999999999_path`` belongs to
    no slot either, because at that index the composer's four bytes hold
    ``999`` and it asks for ``firmware999path``.
    """
    digits = key[len(_FIRMWARE) : -len(_PATH)].rstrip("_")
    if not (digits.isascii() and digits.isdigit()):
        return None
    index = int(digits)
    return index if firmware_key(index, _PATH) == key else None


def _slot_read_by(key: str, limit: int) -> int | None:
    """The slot *key* answers in a run of *limit* slots — ``None`` when no run does.

    Two ways to answer none: the key belongs to no slot at all
    (:func:`_composed_index`), or it belongs to one this run does not reach.
    """
    index = _composed_index(key)
    return index if index is not None and index < limit else None


def unread_reason(key: str, count: str) -> str:
    """Why the enumeration of a file stating *count* passed over *key*.

    One of :data:`UNREAD_NO_SLOT`, :data:`UNREAD_UNCOUNTED`,
    :data:`UNREAD_EMPTY` — asked only about a key :func:`enumerate_firmware`
    put in its ``unread``, and answering the *first* reason the declaration
    failed to reach the emulator. That order is the order upstream fails in: a
    key it never composes is never looked up whatever the count says
    (``core_info.c:1599-1606``), a slot outside the count is never composed,
    and only a key it did look up can have its value discarded for being empty
    (``core_info.c:1610``).

    The count is enough to tell the last two apart because a key that is inside
    the count *and* composed is read unless its value is empty — so a key that
    is both and still unread can only be the empty one.
    """
    index = _composed_index(key)
    if index is None:
        return UNREAD_NO_SLOT
    bound = cfg_uint(count)
    return UNREAD_EMPTY if bound is not None and index < bound else UNREAD_UNCOUNTED


def _slot_at(fields: Mapping[str, str], index: int, path: str) -> FirmwareSlot:
    """Slot *index* as the enumeration fills the rest of it.

    ``firmwareN_opt`` goes through ``config_get_bool``, and a value outside its
    vocabulary is not a false: the write simply does not happen
    (``core_info.c:1603-1604``) and the slot keeps the ``false`` its ``calloc``
    left, which is the *required* end of the scale. So an absent flag, a
    ``TRUE`` and a ``yes`` all mean required — the direction that matters,
    because reading one of them as optional would let an answer go green over a
    file the core will not start without.
    """
    return FirmwareSlot(
        index=index,
        path=path,
        description=fields.get(firmware_key(index, _DESC), ""),
        optional=cfg_bool(fields.get(firmware_key(index, _OPT), "")) is True,
    )


def enumerate_firmware(fields: Mapping[str, str]) -> FirmwareEnumeration:
    """The firmware slots RetroArch enumerates from one ``.info``'s fields.

    ``core_info_resolve_firmware`` (``core_info.c:1572-1629``) reads
    ``firmware_count`` **first** and returns on the spot when that is not an
    unsigned it can read: no count, no firmware — a ``.info`` may list a dozen
    paths and the core is still started without a single one being asked for.
    With a count it allocates exactly that many slots and fills slots
    ``0 .. count-1`` by composing each key (:func:`firmware_key`).

    The count is therefore the enumeration, not a cross-check against it. A
    path declared at or past the count is invisible; a slot inside the count
    that the file says nothing about declares nothing; and a count larger than
    the number of declared paths simply leaves the surplus slots empty.

    That last point is why the walk goes over the paths the file states rather
    than counting up to the bound: a slot the file says nothing about declares
    nothing either way, and a ``firmware_count`` of four billion would
    otherwise be four billion lookups. Upstream never gets there either — it
    ``calloc``s the whole array first and abandons the core's firmware when
    that fails (``core_info.c:1584-1588``).

    Walking the file this way is also what makes the second half of the answer
    possible. Every path key the walk passes over goes into ``unread``, so the
    ways a declaration can be written and still be ignored — a spelling nobody
    composes, a slot nobody reaches, a value the read discards — are three
    outcomes of one loop rather than three checks somebody has to remember to
    write.
    """
    count = fields.get(FIRMWARE_COUNT, "")
    enumerated = cfg_uint(count)
    limit = 0 if enumerated is None else enumerated
    read: list[tuple[int, str]] = []
    unread: list[str] = []
    stating_a_path: list[str] = []
    for key, value in fields.items():
        if not _states_a_path(key):
            continue
        index = _slot_read_by(key, limit)
        if index is not None and value:
            read.append((index, value))
            continue
        unread.append(key)
        if value:
            stating_a_path.append(key)
    slots = tuple(_slot_at(fields, index, path) for index, path in sorted(read))
    return FirmwareEnumeration(slots, count, tuple(sorted(unread)), tuple(sorted(stating_a_path)))
