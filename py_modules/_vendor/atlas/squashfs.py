"""Read one file out of an AppImage's embedded squashfs — stdlib only, capability-gated.

An AppImage (type 2) is an ELF runtime with a squashfs 4.0 image appended;
the image begins where the ELF's section headers end (offset ``e_shoff +
e_shentsize * e_shnum``), which is how the runtime itself locates it. This
module reads exactly one entry out of that image by path — enough to open the
catalogue a frontend ships sealed inside its AppImage (issue #65) — and
nothing more: no listing API, no extraction tree, no write path.

The format facts below are squashfs 4.0 as mksquashfs writes it and the
kernel reads it (little endian throughout):

- the superblock is 96 bytes; ``block_log`` must agree with ``block_size``,
  and the compressor id selects the codec for every compressed block;
- metadata (inodes, directory listings, fragment lookup) lives in blocks of
  at most 8 KiB, each preceded by a ``u16`` whose bit 15 means *uncompressed*
  and whose low bits are the on-disk size; a metadata **reference** is
  ``(block_start << 16) | offset`` with ``block_start`` relative to the
  owning table and ``offset`` into the uncompressed block;
- data blocks are sized by the inode's block list (``u32`` each, bit 24 =
  uncompressed, value 0 = a sparse block of zeros), and a file's tail may
  live inside a shared *fragment* block found through the fragment table;
- directory listings are runs of ``(count-1, inode_block, inode_base)``
  headers followed by entries naming a child and its inode reference.

The zstd codec arrives in the stdlib with Python 3.14 (PEP 784,
``compression.zstd``); the same code is published for older interpreters as
``backports.zstd``, and the probe accepts either — a host application that
vendors the backport grants its runtime the capability. That is discovery,
not a dependency: atlas requires neither. Where no provider exists, an image
compressed with zstd raises :class:`CodecUnavailable`, which the machine seam
reports as the capability being absent — never as the file being absent.
gzip images decompress everywhere through ``zlib``.
"""

from __future__ import annotations

import struct
import zlib
import importlib
from typing import Any, BinaryIO, Callable

_SUPERBLOCK = struct.Struct("<4sIIIIHHHHHHQQQQQQQQ")
_MAGIC = b"hsqs"

_COMPRESSOR_GZIP = 1
_COMPRESSOR_ZSTD = 6

_METADATA_BLOCK = 8192
_METADATA_UNCOMPRESSED = 0x8000
_DATA_UNCOMPRESSED = 1 << 24
_NO_FRAGMENT = 0xFFFFFFFF

# Inode types (squashfs 4.0).
_DIR = 1
_FILE = 2
_SYMLINK = 3
_DIR_EXT = 8
_FILE_EXT = 9
_SYMLINK_EXT = 10

_INODE_HEADER = struct.Struct("<HHHHII")
_DIR_INODE = struct.Struct("<IIHHI")
_DIR_EXT_INODE = struct.Struct("<IIIIHHI")
_FILE_INODE = struct.Struct("<IIII")
_FILE_EXT_INODE = struct.Struct("<QQQIIII")
_SYMLINK_INODE = struct.Struct("<II")
_DIR_HEADER = struct.Struct("<III")
_DIR_ENTRY = struct.Struct("<HhHH")
_FRAGMENT_ENTRY = struct.Struct("<QII")

_MAX_SYMLINK_DEPTH = 8


class SquashfsError(Exception):
    """The bytes are not a squashfs this reader can place — structure, not absence."""


class CodecUnavailable(SquashfsError):
    """The image's codec is real and this interpreter cannot decompress it."""


class EntryNotFound(SquashfsError):
    """The image is fine and carries no entry of that name."""


# The PEP 784 zstd API, in probe order: the stdlib home first (Python 3.14),
# then the published backport of the same code for older interpreters.
_ZSTD_PROVIDERS = ("compression.zstd", "backports.zstd")


def _zstd_module() -> Any:
    for name in _ZSTD_PROVIDERS:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    return None


def _decompressor(compressor: int) -> Callable[[bytes], bytes]:
    if compressor == _COMPRESSOR_GZIP:
        return zlib.decompress
    if compressor == _COMPRESSOR_ZSTD:
        zstd = _zstd_module()
        if zstd is None:
            raise CodecUnavailable(
                "the image is zstd-compressed and this runtime has neither "
                "compression.zstd (Python >= 3.14) nor backports.zstd"
            )
        return zstd.decompress
    raise CodecUnavailable(f"unsupported squashfs compressor id {compressor}")


def _appimage_offset(handle: BinaryIO) -> int:
    """Where the embedded image begins — the end of the ELF's section headers."""
    header = handle.read(64)
    if len(header) < 64 or header[:4] != b"\x7fELF":
        raise SquashfsError("not an ELF — no AppImage runtime to carry an image")
    if header[4] != 2:
        raise SquashfsError("not a 64-bit ELF")
    e_shoff = struct.unpack_from("<Q", header, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", header, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", header, 0x3C)[0]
    if e_shoff == 0 or e_shnum == 0:
        raise SquashfsError("the ELF carries no section headers to append an image after")
    return e_shoff + e_shentsize * e_shnum


class _Image:
    """One opened image: the superblock's facts plus the block-level readers."""

    def __init__(self, handle: BinaryIO, base: int) -> None:
        self._handle = handle
        self._base = base
        handle.seek(base)
        raw = handle.read(_SUPERBLOCK.size)
        if len(raw) < _SUPERBLOCK.size or raw[:4] != _MAGIC:
            raise SquashfsError("no squashfs superblock at the computed offset")
        (
            _,
            _,
            _,
            self.block_size,
            self.fragment_count,
            compressor,
            block_log,
            _,
            _,
            major,
            minor,
            self.root_ref,
            _,
            _,
            _,
            self.inode_table,
            self.directory_table,
            self.fragment_table,
            _,
        ) = _SUPERBLOCK.unpack(raw)
        if (major, minor) != (4, 0):
            raise SquashfsError(f"squashfs {major}.{minor} is not the 4.0 this reader speaks")
        if self.block_size != 1 << block_log:
            raise SquashfsError("superblock block_size and block_log disagree")
        self._decompress = _decompressor(compressor)

    def _read_at(self, offset: int, size: int) -> bytes:
        self._handle.seek(self._base + offset)
        data = self._handle.read(size)
        if len(data) < size:
            raise SquashfsError("the image ends inside a block it declares")
        return data

    def _metadata_block(self, offset: int) -> tuple[bytes, int]:
        """One metadata block at *offset* → (uncompressed bytes, on-disk length)."""
        header = struct.unpack("<H", self._read_at(offset, 2))[0]
        size = header & ~_METADATA_UNCOMPRESSED
        if size > _METADATA_BLOCK:
            raise SquashfsError("metadata block declares more than 8 KiB")
        raw = self._read_at(offset + 2, size)
        if header & _METADATA_UNCOMPRESSED:
            return raw, size + 2
        return self._decompress(raw), size + 2

    def metadata(self, table_start: int, block: int, offset: int, size: int) -> bytes:
        """*size* bytes of a metadata stream, from reference (*block*, *offset*)."""
        out = bytearray()
        position = table_start + block
        skip = offset
        while len(out) < size:
            data, consumed = self._metadata_block(position)
            out.extend(data[skip:])
            skip = 0
            position += consumed
        return bytes(out[:size])

    # ── inodes ──────────────────────────────────────────────────────────

    def _inode_bytes(self, ref: int, size: int) -> bytes:
        return self.metadata(self.inode_table, ref >> 16, ref & 0xFFFF, size)

    def inode(self, ref: int) -> dict[str, Any]:
        """The fields this reader acts on, by inode type — a plain dict on purpose."""
        # Generous fixed read: every inode this reader touches fits in 128
        # bytes plus a block list / target we re-read precisely below.
        head = self._inode_bytes(ref, _INODE_HEADER.size)
        kind = _INODE_HEADER.unpack(head)[0]
        body_ref = ref
        if kind in (_DIR, _DIR_EXT):
            return self._dir_inode(body_ref, kind)
        if kind in (_FILE, _FILE_EXT):
            return self._file_inode(body_ref, kind)
        if kind in (_SYMLINK, _SYMLINK_EXT):
            raw = self._inode_bytes(ref, _INODE_HEADER.size + _SYMLINK_INODE.size)
            target_size = _SYMLINK_INODE.unpack_from(raw, _INODE_HEADER.size)[1]
            raw = self._inode_bytes(
                ref, _INODE_HEADER.size + _SYMLINK_INODE.size + target_size
            )
            return {
                "kind": _SYMLINK,
                "target": raw[_INODE_HEADER.size + _SYMLINK_INODE.size :],
            }
        raise SquashfsError(f"inode type {kind} where a directory, file or symlink was expected")

    def _dir_inode(self, ref: int, kind: int) -> dict[str, Any]:
        if kind == _DIR:
            raw = self._inode_bytes(ref, _INODE_HEADER.size + _DIR_INODE.size)
            block_start, _, file_size, block_offset, _ = _DIR_INODE.unpack_from(
                raw, _INODE_HEADER.size
            )
        else:
            raw = self._inode_bytes(ref, _INODE_HEADER.size + _DIR_EXT_INODE.size)
            _, file_size, block_start, _, _, block_offset, _ = _DIR_EXT_INODE.unpack_from(
                raw, _INODE_HEADER.size
            )
        return {
            "kind": _DIR,
            "block_start": block_start,
            "block_offset": block_offset,
            "file_size": file_size,
        }

    def _file_inode(self, ref: int, kind: int) -> dict[str, Any]:
        if kind == _FILE:
            raw = self._inode_bytes(ref, _INODE_HEADER.size + _FILE_INODE.size)
            blocks_start, fragment, offset, file_size = _FILE_INODE.unpack_from(
                raw, _INODE_HEADER.size
            )
            body_size = _FILE_INODE.size
        else:
            raw = self._inode_bytes(ref, _INODE_HEADER.size + _FILE_EXT_INODE.size)
            blocks_start, file_size, _, _, fragment, offset, _ = _FILE_EXT_INODE.unpack_from(
                raw, _INODE_HEADER.size
            )
            body_size = _FILE_EXT_INODE.size
        if fragment == _NO_FRAGMENT:
            block_count = (file_size + self.block_size - 1) // self.block_size
        else:
            block_count = file_size // self.block_size
        raw = self._inode_bytes(ref, _INODE_HEADER.size + body_size + 4 * block_count)
        block_sizes = struct.unpack_from(
            f"<{block_count}I", raw, _INODE_HEADER.size + body_size
        )
        return {
            "kind": _FILE,
            "blocks_start": blocks_start,
            "fragment": fragment,
            "offset": offset,
            "file_size": file_size,
            "block_sizes": block_sizes,
        }

    # ── directories ─────────────────────────────────────────────────────

    def child_ref(self, directory: dict[str, Any], name: bytes) -> int | None:
        """The inode reference *name* resolves to in *directory*, or ``None``."""
        listing_size = int(directory["file_size"]) - 3
        if listing_size <= 0:
            return None
        listing = self.metadata(
            self.directory_table,
            int(directory["block_start"]),
            int(directory["block_offset"]),
            listing_size,
        )
        position = 0
        while position < len(listing):
            count, inode_block, _ = _DIR_HEADER.unpack_from(listing, position)
            position += _DIR_HEADER.size
            for _ in range(count + 1):
                offset, _, _, name_size = _DIR_ENTRY.unpack_from(listing, position)
                position += _DIR_ENTRY.size
                entry_name = listing[position : position + name_size + 1]
                position += name_size + 1
                if entry_name == name:
                    return (inode_block << 16) | offset
        return None

    # ── file data ───────────────────────────────────────────────────────

    def _fragment_entry(self, index: int) -> tuple[int, int]:
        """The fragment block's (start, size-word) for *index*."""
        if index >= self.fragment_count:
            raise SquashfsError("fragment index past the table")
        per_block = _METADATA_BLOCK // _FRAGMENT_ENTRY.size
        pointer_offset = (index // per_block) * 8
        pointer = struct.unpack(
            "<Q", self._read_at(self.fragment_table + pointer_offset, 8)
        )[0]
        # The pointer on disk is archive-relative, like every other offset here.
        block, _ = self._metadata_block(pointer)
        start, size, _ = _FRAGMENT_ENTRY.unpack_from(
            block, (index % per_block) * _FRAGMENT_ENTRY.size
        )
        return start, size

    def _data_block(self, offset: int, size_word: int, expected: int) -> bytes:
        if size_word == 0:
            return b"\x00" * expected
        raw = self._read_at(offset, size_word & ~_DATA_UNCOMPRESSED)
        if size_word & _DATA_UNCOMPRESSED:
            return raw
        return self._decompress(raw)

    def file_bytes(self, inode: dict[str, Any]) -> bytes:
        out = bytearray()
        file_size = int(inode["file_size"])
        position = int(inode["blocks_start"])
        remaining = file_size
        for size_word in inode["block_sizes"]:
            expected = min(self.block_size, remaining)
            data = self._data_block(position, int(size_word), expected)
            out.extend(data[:expected])
            position += int(size_word) & ~_DATA_UNCOMPRESSED
            remaining -= expected
        if remaining:
            fragment = int(inode["fragment"])
            if fragment == _NO_FRAGMENT:
                raise SquashfsError("file data ends before its declared size")
            start, size_word = self._fragment_entry(fragment)
            block = self._data_block(start, size_word, self.block_size)
            tail_offset = int(inode["offset"])
            out.extend(block[tail_offset : tail_offset + remaining])
        if len(out) != file_size:
            raise SquashfsError("file data ends before its declared size")
        return bytes(out)


def _relinked(components: list[str], index: int, target: str) -> list[str]:
    """The component list after the link at *index* is replaced by *target*.

    An absolute target restarts from the root; a relative one resolves
    against the link's own directory, with ``..`` stepping back through that
    prefix and never past the root. Either way the components after the link
    stay — the walk restarts from the root over the rewritten list.
    """
    target_parts = [part for part in target.split("/") if part and part != "."]
    rest = components[index + 1 :]
    if target.startswith("/"):
        return target_parts + rest
    prefix = components[:index]
    for part in target_parts:
        if part == "..":
            if not prefix:
                raise SquashfsError("symlink escapes the image root")
            prefix.pop()
        else:
            prefix.append(part)
    return prefix + rest


def _child_inode(image: _Image, node: dict[str, Any], components: list[str], index: int) -> dict[str, Any]:
    """The inode *components[index]* names inside *node*, refusals spelled out."""
    if node["kind"] != _DIR:
        raise EntryNotFound(f"{'/'.join(components[:index])!r} is not a directory")
    ref = image.child_ref(node, components[index].encode("utf-8"))
    if ref is None:
        raise EntryNotFound(f"no entry {components[index]!r} in the image")
    return image.inode(ref)


def _resolve(image: _Image, inner_path: str) -> bytes:
    """Walk *inner_path* from the root, following in-image symlinks, and read it."""
    components = [part for part in inner_path.split("/") if part]
    if not components:
        raise EntryNotFound("an empty entry path names nothing")
    followed = 0
    node = image.inode(image.root_ref)
    index = 0
    while index < len(components):
        child = _child_inode(image, node, components, index)
        if child["kind"] == _SYMLINK:
            followed += 1
            if followed > _MAX_SYMLINK_DEPTH:
                raise SquashfsError("symlink chain deeper than the reader follows")
            target = bytes(child["target"]).decode("utf-8", errors="replace")
            components = _relinked(components, index, target)
            node = image.inode(image.root_ref)
            index = 0
            continue
        node = child
        index += 1
    if node["kind"] != _FILE:
        raise EntryNotFound(f"{inner_path!r} is not a regular file in the image")
    return image.file_bytes(node)


def read_appimage_entry(path: str, inner_path: str) -> bytes:
    """The bytes of *inner_path* inside the AppImage at *path*.

    Raises ``OSError`` for anything the filesystem refuses (missing file
    included — the caller tells those apart), :class:`SquashfsError` when the
    file is not an AppImage-with-squashfs this reader can place,
    :class:`CodecUnavailable` when the interpreter lacks the image's codec,
    and :class:`EntryNotFound` when the image is fine and the entry is not
    in it.
    """
    with open(path, "rb") as handle:
        offset = _appimage_offset(handle)
        handle.seek(0)
        image = _Image(handle, offset)
        return _resolve(image, inner_path)
