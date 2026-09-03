"""Read a PS2 BIOS image's ROMDIR header the way LRPS2 reads it — stdlib only.

The core keeps no table of BIOS images: a file in its ``pcsx2/bios`` folder is
a BIOS when ``IsBIOS`` says so (github.com/libretro/ps2 @ 14d19f8,
``pcsx2/ps2/BiosTools.cpp:325-335``), and ``IsBIOS`` opens the file and hands
it to ``LoadBiosVersion`` (:60-173), a structural read of the ROMDIR table at
the front of an image. This module performs that read, step for step, so a
folder candidate is judged by the test the core applies rather than by a hash
table listing a subset of what the test accepts.

What ``LoadBiosVersion`` does, at 14d19f8:

- a ROMDIR record is 16 packed bytes — ``char fileName[10]; u16 extInfoSize;
  u32 fileSize`` (:38-47), little endian;
- records are read from offset 0, one after another, up to ``512 * 1024`` of
  them, until one is named ``RESET`` (:63-70); a file that ends before that is
  rejected (:65-66). The record need not be the first — whatever sits ahead of
  it is read past, and the loop's bound is the only limit — and when the bound
  runs out without a ``RESET`` the walk below starts from the last record read,
  because nothing between the loop and the walk tells the two apart;
- the table is then walked while the record's name is non-empty and
  NUL-terminated within its ten bytes (:79). ``EXTINFO`` reads 15 bytes at
  ``fileOffset + 0x10`` as the serial (:81-88); ``ROMVER`` reads 14 bytes at
  ``fileOffset`` as the version string, and is what makes the file a BIOS
  (:90-99, :112, :162-163, :172); either read falling short ends the walk
  (:86, :96). ``fileOffset`` advances by the record's ``fileSize`` rounded up to
  16 (:101-104) and the next record is read (:106-107), a short read ending
  the walk; afterwards the padding of the record the walk stopped on is taken
  back (:110);
- the version string is fourteen bytes: ``romver[0:2]`` major, ``romver[2:4]``
  minor, ``romver[4]`` the zone letter, ``romver[5]`` ``C`` for a console or
  ``D`` for a devel unit, ``romver[6:10]`` year, ``romver[10:12]`` month,
  ``romver[12:14]`` day (:114-155). The zone letter maps to a word and a region
  number (:117-133) — ``J`` Japan 0, ``A`` USA 1, ``E`` Europe 2, ``H`` Asia
  4, ``C`` China 6, ``T`` COH-H when ``romver[5]`` is ``Z`` and T10K otherwise
  8, ``X`` Test 9, ``P`` Free 10 — and any other letter is the zone itself
  with region 0 (:129-133);
- the description the core logs, and shows as the ``pcsx2_bios`` option
  label (``libretro/main.cpp:1819``, ``:1829``), is
  ``"%-7s v%s.%s(%c%c/%c%c/%c%c%c%c)  %s %s"`` over zone, major, minor, day,
  month, year, ``Console`` / ``Devel`` / nothing, and the serial (:147-155) —
  two spaces before the console word. A file shorter than the walked offset
  gets ``" %d%%"`` appended, its size as a percentage of that offset
  (:165-170).

Two things the C does that this reader does not reproduce: the ``strtol``
packing of the two version pairs into one number (:157-158; the pairs
themselves are carried), and the partial fill ``fread`` leaves in a record or
version buffer on a short read — the reader keeps the last complete record
where the core's buffer would hold a mixture, which touches only the
percentage suffix and a version read after an earlier ``ROMVER``.

Bytes are carried as ``latin-1`` — one byte, one character — so a description
is the bytes the core's format wrote, and the ``%s`` arguments stop at the
first NUL the way a C string does.
"""

from __future__ import annotations

import dataclasses
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO

# ``struct romdir`` (:38-47), packed: ten name bytes, a u16, a u32.
_ROMDIR = struct.Struct("<10sHI")
RECORD_SIZE = _ROMDIR.size
# How many records the RESET loop reads before it stops looking (:63).
RESET_SCAN_BOUND = 512 * 1024
ROMVER_LENGTH = 14
SERIAL_LENGTH = 15
_SERIAL_OFFSET = 0x10
_ALIGNMENT = 0x10
_U32 = 0xFFFFFFFF
_RESET = b"RESET"
_EXTINFO = b"EXTINFO"
_ROMVER = b"ROMVER"
# The zone letters the switch names (:117-127), minus ``T``, which reads a
# second byte to choose between two words (:125).
_ZONES: dict[bytes, tuple[str, int]] = {
    b"J": ("Japan", 0),
    b"A": ("USA", 1),
    b"E": ("Europe", 2),
    b"H": ("Asia", 4),
    b"C": ("China", 6),
    b"X": ("Test", 9),
    b"P": ("Free", 10),
}
_ZONE_T_REGION = 8


class NotAPs2Bios(Exception):
    """The bytes fail the core's own test — the message says at which step, the seam says only that they fail."""


@dataclass(frozen=True, slots=True)
class Ps2BiosHeader:
    """What ``LoadBiosVersion`` extracts from an image it accepts (BiosTools.cpp:60-173 at 14d19f8).

    ``zone`` and ``region`` are the word and the number the zone letter maps
    to (:117-133); ``major`` / ``minor`` are the two version pairs as the
    core's own strings (:145-146); ``year`` / ``month`` / ``day`` are the date
    bytes in the order the version string carries them; ``build`` is
    ``Console``, ``Devel`` or empty (:153-154); ``serial`` is the ``EXTINFO``
    string, empty where the walk met none (:87, and :327 where ``IsBIOS``
    starts it empty); ``description`` is the core's format over all of them
    (:147-155), plus the truncation suffix where the core appends one
    (:165-170).
    """

    zone: str
    region: int
    major: str
    minor: str
    year: str
    month: str
    day: str
    build: str
    serial: str
    description: str

    @property
    def version(self) -> str:
        """``major.minor`` — the ``v02.00`` of the description without its ``v``."""
        return f"{self.major}.{self.minor}"

    @property
    def date(self) -> str:
        """``year-month-day`` — the version string's own order, with separators."""
        return f"{self.year}-{self.month}-{self.day}"

    @classmethod
    def from_strings(cls, romver: bytes, serial: bytes) -> Ps2BiosHeader:
        """The fields and the description over the two strings the walk yields (:114-155).

        *romver* is the fourteen version bytes as read (:95); *serial* is the
        ``EXTINFO`` string as C reads it (:87), so at most fifteen bytes and
        without a NUL. No truncation suffix — that needs the file, and
        :func:`read_header` appends it where the core would.
        """
        letter = romver[4:5]
        if letter == b"T":
            zone, region = ("COH-H" if romver[5:6] == b"Z" else "T10K"), _ZONE_T_REGION
        elif letter in _ZONES:
            zone, region = _ZONES[letter]
        else:
            # ``zone += romver[4]`` (:131): one byte, printed through ``%s``,
            # so a NUL byte prints as nothing.
            zone, region = _text(_c_string(letter)), 0
        major = _text(_c_string(romver[0:2]))
        minor = _text(_c_string(romver[2:4]))
        build = _build_word(romver[5:6])
        year, month, day = _text(romver[6:10]), _text(romver[10:12]), _text(romver[12:14])
        serial_text = _text(serial)
        # ``"%-7s v%s.%s(%c%c/%c%c/%c%c%c%c)  %s %s"`` (:147-155): the zone
        # left-justified in seven, the date as day/month/year, two spaces
        # before the console word.
        description = f"{zone:<7} v{major}.{minor}({day}/{month}/{year})  {build} {serial_text}"
        return cls(
            zone=zone,
            region=region,
            major=major,
            minor=minor,
            year=year,
            month=month,
            day=day,
            build=build,
            serial=serial_text,
            description=description,
        )


@dataclass(frozen=True, slots=True)
class _Walk:
    """What the table walk (:79-110) came back with."""

    romver: bytes | None
    serial: bytes
    file_offset: int


def _text(raw: bytes) -> str:
    return raw.decode("latin-1")


def _build_word(byte: bytes) -> str:
    """``romver[5]`` as the format prints it (:153-154): ``Console`` for ``C``, ``Devel`` for ``D``, else nothing."""
    if byte == b"C":
        return "Console"
    if byte == b"D":
        return "Devel"
    return ""


def _c_string(raw: bytes) -> bytes:
    """*raw* as ``%s`` prints it: up to the first NUL."""
    end = raw.find(b"\x00")
    return raw if end < 0 else raw[:end]


def _named(name: bytes, literal: bytes) -> bool:
    """``strncmp(name, literal, 10) == 0`` (:68, :81, :90): the literal and its terminator, byte for byte."""
    return name[: len(literal) + 1] == literal + b"\x00"


def _rounded(size: int) -> int:
    """``(fileSize + 0x10) & 0xfffffff0`` (:104, :110) in the core's u32 arithmetic — the mask is also the wrap."""
    return (size + _ALIGNMENT) & 0xFFFFFFF0


def _as_c_int(value: int) -> int:
    """*value* through the ``(int)`` cast at :165 and :167 — two's complement, 32 bits."""
    return ((value + 0x80000000) & _U32) - 0x80000000


def _size_of(handle: BinaryIO) -> int:
    """``FileSystem::FSize64`` (:73): the end of the file, with the position kept."""
    position = handle.tell()
    size = handle.seek(0, os.SEEK_END)
    handle.seek(position)
    return size


def _read_at(handle: BinaryIO, offset: int, length: int) -> bytes | None:
    """The seek-read-seek-back over an ``EXTINFO`` or ``ROMVER`` record (:83-86, :93-96).

    ``None`` where the read falls short, which is where the core breaks out
    of the walk. A seek past the end of a regular file succeeds on both
    sides, so the read is the step that can fail.
    """
    position = handle.tell()
    handle.seek(offset)
    data = handle.read(length)
    if len(data) != length:
        return None
    handle.seek(position)
    return data


def _find_reset(handle: BinaryIO) -> tuple[bytes, int | None]:
    """The ``RESET`` loop (:63-70): the record it stops on, and the index of the ``RESET`` record.

    The index is ``None`` where the bound ran out first; the record is then
    the last one read, and the walk starts from it the way the core's does.
    """
    raw = b""
    for index in range(RESET_SCAN_BOUND):
        raw = handle.read(RECORD_SIZE)
        if len(raw) != RECORD_SIZE:
            raise NotAPs2Bios(
                f"the file ends after {index} complete ROMDIR records without a RESET record — rejected "
                "where the core rejects it (pcsx2/ps2/BiosTools.cpp:65-66 at 14d19f8)"
            )
        if _named(raw, _RESET):
            return raw, index
    return raw, None


def _record_strings(
    handle: BinaryIO, name: bytes, file_offset: int, serial: bytes, romver: bytes | None
) -> tuple[bytes, bytes | None] | None:
    """One record's ``EXTINFO`` and ``ROMVER`` reads (:81-99): the two strings after it.

    ``None`` where a read fell short (:86, :96) — the walk ends there, and
    the strings stay what they were.
    """
    if _named(name, _EXTINFO):
        data = _read_at(handle, file_offset + _SERIAL_OFFSET, SERIAL_LENGTH)
        if data is None:
            return None
        serial = _c_string(data)
    if _named(name, _ROMVER):
        data = _read_at(handle, file_offset, ROMVER_LENGTH)
        if data is None:
            return None
        romver = data
    return serial, romver


def _walk_table(handle: BinaryIO, record: bytes) -> _Walk:
    """The table walk (:79-110) from *record*: the two strings, and the offset the walk reached."""
    romver: bytes | None = None
    serial = b""
    file_offset = 0
    name, _ext_info_size, file_size = _ROMDIR.unpack(record)
    # ``fileName[0] != '\0' && strnlen(fileName, 10) != 10`` (:79).
    while name[0] != 0 and b"\x00" in name:
        strings = _record_strings(handle, name, file_offset, serial, romver)
        if strings is None:
            break
        serial, romver = strings
        # :101-104, then the next record (:106-107).
        file_offset += file_size if file_size % _ALIGNMENT == 0 else _rounded(file_size)
        raw = handle.read(RECORD_SIZE)
        if len(raw) != RECORD_SIZE:
            break
        name, _ext_info_size, file_size = _ROMDIR.unpack(raw)
    file_offset -= (_rounded(file_size) - file_size) & _U32
    return _Walk(romver, serial, file_offset)


def read_header(handle: BinaryIO) -> Ps2BiosHeader:
    """``LoadBiosVersion`` over *handle*, positioned at 0 (BiosTools.cpp:60-173 at 14d19f8).

    Raises :class:`NotAPs2Bios` where the core returns ``false``: the file
    ends before a ``RESET`` record (:65-66), or the walk finds no ``ROMVER``
    (:162-163). Reads what the core reads and no more — sixteen bytes at a
    time up to the bound, then the table, then the two strings.
    """
    record, reset_index = _find_reset(handle)
    file_size = _size_of(handle)
    walk = _walk_table(handle, record)
    if walk.romver is None:
        where = (
            f"the RESET record is record {reset_index}"
            if reset_index is not None
            else f"no RESET record among the first {RESET_SCAN_BOUND} records"
        )
        raise NotAPs2Bios(
            f"{where} and the table walked from there yielded no ROMVER string — it names no ROMVER record, "
            "or that record's bytes fell short — rejected where the core rejects it "
            "(pcsx2/ps2/BiosTools.cpp:162-163 at 14d19f8)"
        )
    header = Ps2BiosHeader.from_strings(walk.romver, walk.serial)
    offset = _as_c_int(walk.file_offset)
    if file_size < offset:
        # ``" %d%%"`` (:165-170): the file is shorter than the table says.
        header = dataclasses.replace(header, description=f"{header.description} {file_size * 100 // offset}%")
    return header
