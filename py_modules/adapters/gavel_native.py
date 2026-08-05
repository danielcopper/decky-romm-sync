"""Native gavel core adapter — the compiled save-sync decision kernels.

The single seam through which save-sync reaches the compiled
`romm-gavel <https://github.com/danielcopper/romm-gavel>`_ core for both of its
decisions: the full per-``(rom, filename, slot)`` sync action and the upload-409
resolution fallback. Owns the ``ctypes`` load of the vendored
``py_modules/native/libgavel-x86_64-linux.so`` and the FFI marshalling around its C
ABI. The adapter is itself the callable implementing the
``ResolveUploadConflictFn`` Protocol, and its :meth:`compute_sync_action` bound
method implements ``ComputeSyncActionFn``, so services consume both decisions
without knowing a shared library is behind them.

Two conversions are the adapter's job rather than the core's, and both exist so
"unknown" survives the boundary as a flag instead of a value:

- **Timestamps.** The core takes epoch seconds plus a "known" companion flag;
  turning a server save's ISO-8601 ``updated_at`` into that pair happens here.
  What the *contract* says — an unparseable timestamp loses head selection and
  cannot prove local-newer — lives in the core, as its behavior when the flag is
  clear. A substitute instant would silently make that contract untestable.
- **Optional numbers.** An absent size becomes a clear ``has_*`` flag, never a
  sentinel value: 0 is exactly what the corrupt-local guard reacts to, so it
  cannot double as "no size recorded".

Presence of the local file is carried by the pointer alone (``NULL`` vs.
non-``NULL``), never by the ``has_*`` flags — a file that exists but could not be
measured is a real case the decision must keep apart from a missing one.

There is no fallback: if the library cannot load, :class:`GavelNativeLoadError`
propagates so bootstrap aborts and the plugin stays inert — the same
"fatal until the environment is fixed" posture as the SQLite migration gate.
Nothing in-tree stands in for the core; :mod:`domain.sync_action` holds only the
vocabulary the answer comes back in.
"""

from __future__ import annotations

import ctypes
import os
from typing import Any, Literal

from domain.iso_time import parse_iso_to_epoch
from domain.sync_action import Conflict, Download, Skip, SyncAction, Upload

# Module-relative path to the vendored shared object, mirroring
# ``adapters.sqlite_migrations.MIGRATIONS_DIR``: resolving off ``__file__``
# (not the plugin dir) locates the artifact identically in the installed
# plugin (``<plugin>/py_modules/native/…``) and in the repo checkout tests
# run from, so the no-fallback load succeeds in both.
_BUNDLED_LIB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "native", "libgavel-x86_64-linux.so")
)

# ``gavel_action`` / ``gavel_skip_reason`` enumerator values (core/gavel.h).
_ACTION_SKIP = 0
_ACTION_UPLOAD = 1
_ACTION_DOWNLOAD = 2
_ACTION_CONFLICT = 3
_SKIP_REASONS = {0: "synced", 1: "nothing_to_sync"}


class GavelNativeLoadError(RuntimeError):
    """The vendored gavel native core could not be loaded.

    Raised by :class:`GavelNativeAdapter` when ``ctypes`` cannot open or bind
    the shared object. The message names the path that was tried so a missing
    or wrong-architecture artifact is diagnosable from the log alone.
    """


class _DeviceSync(ctypes.Structure):
    """``gavel_device_sync`` — one device's sync record on a server save."""

    _fields_ = (("device_id", ctypes.c_char_p), ("is_current", ctypes.c_int))


class _ServerSave(ctypes.Structure):
    """``gavel_server_save`` — one RomM save in the slot."""

    _fields_ = (
        ("id", ctypes.c_int64),
        ("updated_at_epoch", ctypes.c_double),
        ("has_updated_at", ctypes.c_int),
        ("content_hash", ctypes.c_char_p),
        ("device_syncs", ctypes.POINTER(_DeviceSync)),
        ("device_sync_count", ctypes.c_size_t),
    )


class _LocalFile(ctypes.Structure):
    """``gavel_local_file`` — the local file's measurements, when one exists."""

    _fields_ = (
        ("size", ctypes.c_int64),
        ("has_size", ctypes.c_int),
        ("mtime", ctypes.c_double),
        ("has_mtime", ctypes.c_int),
    )


class _Bookkeeping(ctypes.Structure):
    """``gavel_bookkeeping`` — this device's recorded state for the file."""

    _fields_ = (
        ("last_sync_hash", ctypes.c_char_p),
        ("last_sync_server_hash", ctypes.c_char_p),
        ("last_sync_local_size", ctypes.c_int64),
        ("has_last_sync_local_size", ctypes.c_int),
    )


class _SyncActionResult(ctypes.Structure):
    """``gavel_sync_action`` — the out-parameter the decision is written into."""

    _fields_ = (
        ("action", ctypes.c_int),
        ("reason", ctypes.c_int),
        ("adopt_baseline", ctypes.c_int),
        ("target_save_id", ctypes.c_int64),
        ("has_target_save_id", ctypes.c_int),
        ("server_save_id", ctypes.c_int64),
    )


class GavelNativeAdapter:
    """Both save-sync decision kernels, backed by the compiled gavel core.

    Loads the shared object and binds ``gavel_resolve_upload_conflict`` and
    ``gavel_compute_sync_action`` at construction; a load failure raises
    :class:`GavelNativeLoadError` rather than degrading.

    Calling the adapter is the ``ResolveUploadConflictFn`` seam: it marshals the
    four hash arguments to the C ABI (``None`` → ``NULL``, ``str`` → UTF-8 bytes —
    the empty string stays a distinct empty value, never collapsed to ``NULL``)
    and maps the ``int`` result to the decision string (``0`` → ``"download"``,
    anything else → ``"conflict"``). :meth:`compute_sync_action` is the
    ``ComputeSyncActionFn`` seam and answers in this codebase's own
    :class:`~domain.sync_action.SyncAction` dataclasses.
    """

    def __init__(self, lib_path: str = _BUNDLED_LIB_PATH) -> None:
        try:
            self._lib = ctypes.CDLL(lib_path)
        except OSError as exc:
            raise GavelNativeLoadError(f"failed to load gavel native core from {lib_path!r}: {exc}") from exc
        # Declaring argtypes/restype is not optional politeness: without them
        # ctypes assumes every argument and the result are C ints, which
        # truncates pointers on the way in and misreads what comes back.
        self._resolve = self._bind(lib_path, "gavel_resolve_upload_conflict")
        self._resolve.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self._resolve.restype = ctypes.c_int
        self._compute = self._bind(lib_path, "gavel_compute_sync_action")
        self._compute.argtypes = [
            ctypes.POINTER(_LocalFile),
            ctypes.POINTER(_ServerSave),
            ctypes.c_size_t,
            ctypes.POINTER(_Bookkeeping),
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(_SyncActionResult),
        ]
        self._compute.restype = None

    def _bind(self, lib_path: str, symbol: str) -> Any:
        """Resolve *symbol* in the loaded library, or fail the load loudly."""
        try:
            return getattr(self._lib, symbol)
        except AttributeError as exc:
            raise GavelNativeLoadError(f"gavel native core at {lib_path!r} is missing symbol {symbol!r}") from exc

    def __call__(
        self,
        local_hash: str | None,
        last_sync_hash: str | None,
        server_content_hash: str | None,
        last_sync_server_hash: str | None,
    ) -> Literal["download", "conflict"]:
        result = self._resolve(
            _encode(local_hash),
            _encode(last_sync_hash),
            _encode(server_content_hash),
            _encode(last_sync_server_hash),
        )
        return "download" if result == 0 else "conflict"

    def compute_sync_action(
        self,
        local_file: dict[str, Any] | None,
        server_saves_in_slot: list[dict[str, Any]],
        files_state: dict[str, Any],
        device_id: str,
        local_hash: str | None,
    ) -> SyncAction:
        """Compute the sync action for a single ``(rom, filename, slot)`` triple.

        Takes the raw shapes the caller already holds — ``local_file``
        (``None`` when the file does not exist locally), the slot's RomM server
        saves already filtered by the caller, the per-filename slice of recorded
        sync state, this device's id, and the local content hash — and answers
        with a :class:`~domain.sync_action.SyncAction`.

        Every server save must carry an integer ``id`` (RomM's primary key): the
        core addresses its chosen save by that id, and it is what a returned
        ``Download`` / ``Conflict`` is resolved back through to the caller's own
        save dict.
        """
        # Every buffer the C structs point at has to outlive the call. CPython's
        # ctypes does track this itself — a value assigned to a pointer field is
        # recorded in the container's ``_objects``, including through an array
        # element (``arr[i].field = b"…"``) — but that is an implementation
        # detail of one interpreter, not a documented guarantee. Holding each
        # encoded string and each array here until the call returns is what this
        # adapter actually relies on.
        keepalive: list[object] = []
        saves, saves_by_id = _build_saves(server_saves_in_slot, keepalive)
        result = _SyncActionResult()
        self._compute(
            _build_local_file(local_file),
            saves,
            len(server_saves_in_slot),
            _build_bookkeeping(files_state, keepalive),
            _encode(device_id),
            _encode(local_hash),
            ctypes.byref(result),
        )
        return _decode_action(result, saves_by_id)


def _encode(value: str | None) -> bytes | None:
    """Marshal a hash argument to the C ABI, preserving ``None`` vs ``""``.

    ``None`` becomes ``NULL`` (unknown); a string — including the empty
    string — becomes its UTF-8 bytes. The kernel treats ``NULL`` and ``""``
    identically as "unknown", but the distinction must survive the boundary
    so the marshalling never invents information the caller did not supply.
    """
    return None if value is None else value.encode("utf-8")


def _optional_timestamp(value: object) -> tuple[float, int]:
    """Marshal an optional epoch-seconds value to the core's ``(mtime, has_mtime)`` pair.

    A number is present; anything that is not one — an absent key, ``None``, a
    string — is absent, never a coerced or substitute instant.
    """
    if isinstance(value, int | float):
        return float(value), 1
    return 0.0, 0


def _optional_size(value: object, field: str) -> tuple[int, int]:
    """Marshal an optional byte count to the core's ``(size, has_size)`` pair.

    Sizes are whole numbers of bytes and the ABI carries them as ``int64_t``. A
    value that is not integral cannot be represented, and rounding it would land
    on 0 — the one value the corrupt-local guard reacts to — so it raises rather
    than quietly answering a different question. Absent stays absent.
    """
    if value is None:
        return 0, 0
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return int(value), 1
    raise ValueError(f"{field} must be a whole number of bytes, got {value!r}")


def _build_local_file(local_file: dict[str, Any] | None) -> Any:
    """Marshal the local file to a struct pointer, or ``NULL`` when it does not exist.

    Presence rides on the pointer alone: any dict — including one whose size and
    mtime are both unknown — is a file that exists and could not be measured,
    which the decision keeps apart from a missing file.
    """
    if local_file is None:
        return None
    size, has_size = _optional_size(local_file.get("size"), "local_file.size")
    mtime, has_mtime = _optional_timestamp(local_file.get("mtime"))
    return ctypes.pointer(_LocalFile(size=size, has_size=has_size, mtime=mtime, has_mtime=has_mtime))


def _build_bookkeeping(files_state: dict[str, Any], keepalive: list[object]) -> Any:
    """Marshal the recorded per-file sync state to a struct pointer.

    An empty ``files_state`` is still a record, not an absent one: a missing key
    is a value this device never recorded, and a struct whose every field reads
    as unknown says exactly that.
    """
    last_sync_hash = _encode(files_state.get("last_sync_hash"))
    last_sync_server_hash = _encode(files_state.get("last_sync_server_hash"))
    keepalive.extend((last_sync_hash, last_sync_server_hash))
    size, has_size = _optional_size(files_state.get("last_sync_local_size"), "files_state.last_sync_local_size")
    return ctypes.pointer(
        _Bookkeeping(
            last_sync_hash=last_sync_hash,
            last_sync_server_hash=last_sync_server_hash,
            last_sync_local_size=size,
            has_last_sync_local_size=has_size,
        )
    )


def _build_saves(
    server_saves_in_slot: list[dict[str, Any]],
    keepalive: list[object],
) -> tuple[Any, dict[int, dict[str, Any]]]:
    """Marshal the slot's server saves into one contiguous array.

    Returns the array (``NULL`` for an empty slot) plus the id → save-dict index
    built from the very ids written into it, so the core's chosen save resolves
    back to the caller's own dict without a second pass over the list.
    """
    saves_by_id: dict[int, dict[str, Any]] = {}
    if not server_saves_in_slot:
        return None, saves_by_id

    saves = (_ServerSave * len(server_saves_in_slot))()
    keepalive.append(saves)
    for index, save in enumerate(server_saves_in_slot):
        device_syncs = save.get("device_syncs") or []
        entries = (_DeviceSync * len(device_syncs))()
        keepalive.append(entries)
        for entry_index, entry in enumerate(device_syncs):
            entry_device_id = _encode(entry.get("device_id"))
            keepalive.append(entry_device_id)
            entries[entry_index].device_id = entry_device_id
            entries[entry_index].is_current = 1 if entry.get("is_current") else 0

        content_hash = _encode(save.get("content_hash"))
        keepalive.append(content_hash)
        epoch = parse_iso_to_epoch(save.get("updated_at"))
        save_id = int(save["id"])
        saves_by_id[save_id] = save
        saves[index].id = save_id
        saves[index].updated_at_epoch = 0.0 if epoch is None else epoch
        saves[index].has_updated_at = 0 if epoch is None else 1
        saves[index].content_hash = content_hash
        saves[index].device_syncs = ctypes.cast(entries, ctypes.POINTER(_DeviceSync))
        saves[index].device_sync_count = len(device_syncs)
    return saves, saves_by_id


def _decode_action(result: _SyncActionResult, saves_by_id: dict[int, dict[str, Any]]) -> SyncAction:
    """Map the C result struct onto this codebase's ``SyncAction`` dataclasses.

    ``Download`` and ``Conflict`` carry the whole server-save dict their
    consumers read (``id``, ``updated_at``, ``content_hash``,
    ``file_size_bytes``), so the chosen ``server_save_id`` is resolved back
    through *saves_by_id* — the same dicts the caller passed in.
    """
    if result.action == _ACTION_SKIP:
        return Skip(reason=_SKIP_REASONS[result.reason], adopt_baseline=bool(result.adopt_baseline))
    if result.action == _ACTION_UPLOAD:
        return Upload(target_save_id=result.target_save_id if result.has_target_save_id else None)
    if result.action not in (_ACTION_DOWNLOAD, _ACTION_CONFLICT):
        # An action this adapter does not know means the vendored core and this
        # code disagree about the ABI. Falling through to Conflict would hide
        # that behind a plausible-looking decision; the posture everywhere else
        # in this module is to fail loudly rather than substitute something.
        raise ValueError(f"gavel native core returned unknown action {result.action}")
    server_save = saves_by_id[result.server_save_id]
    if result.action == _ACTION_DOWNLOAD:
        return Download(server_save=server_save)
    return Conflict(server_save=server_save)
