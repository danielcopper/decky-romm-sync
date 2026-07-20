"""Native gavel core adapter — the compiled 409-resolution kernel.

The single seam through which save-sync reaches the compiled
`romm-gavel <https://github.com/danielcopper/romm-gavel>`_ core for its
upload-409 resolution decision. Owns the ``ctypes`` load of the vendored
``py_modules/native/libgavel-x86_64-linux.so`` and the FFI marshalling around its C
ABI. The adapter is itself the callable implementing the
``ResolveUploadConflictFn`` Protocol, so services consume the decision
without knowing a shared library is behind it.

There is no fallback: if the library cannot load, :class:`GavelNativeLoadError`
propagates so bootstrap aborts and the plugin stays inert — the same
"fatal until the environment is fixed" posture as the SQLite migration
gate. The in-tree :func:`domain.sync_action.resolve_upload_conflict` kernel
is not a runtime fallback; it survives only as the differential oracle in
tests.
"""

from __future__ import annotations

import ctypes
import os
from typing import Literal

# Module-relative path to the vendored shared object, mirroring
# ``adapters.sqlite_migrations.MIGRATIONS_DIR``: resolving off ``__file__``
# (not the plugin dir) locates the artifact identically in the installed
# plugin (``<plugin>/py_modules/native/…``) and in the repo checkout tests
# run from, so the no-fallback load succeeds in both.
_BUNDLED_LIB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "native", "libgavel-x86_64-linux.so")
)


class GavelNativeLoadError(RuntimeError):
    """The vendored gavel native core could not be loaded.

    Raised by :class:`GavelNativeAdapter` when ``ctypes`` cannot open or bind
    the shared object. The message names the path that was tried so a missing
    or wrong-architecture artifact is diagnosable from the log alone.
    """


class GavelNativeAdapter:
    """Real ``ResolveUploadConflictFn`` backed by the compiled gavel core.

    Loads the shared object and binds ``gavel_resolve_upload_conflict`` at
    construction; a load failure raises :class:`GavelNativeLoadError` rather
    than degrading. Calling the adapter marshals the four hash arguments to
    the C ABI (``None`` → ``NULL``, ``str`` → UTF-8 bytes — the empty string
    stays a distinct empty value, never collapsed to ``NULL``) and maps the
    ``int`` result to the decision string (``0`` → ``"download"``, anything
    else → ``"conflict"``).
    """

    def __init__(self, lib_path: str = _BUNDLED_LIB_PATH) -> None:
        try:
            self._lib = ctypes.CDLL(lib_path)
        except OSError as exc:
            raise GavelNativeLoadError(f"failed to load gavel native core from {lib_path!r}: {exc}") from exc
        try:
            self._resolve = self._lib.gavel_resolve_upload_conflict
        except AttributeError as exc:
            raise GavelNativeLoadError(
                f"gavel native core at {lib_path!r} is missing symbol 'gavel_resolve_upload_conflict'"
            ) from exc
        self._resolve.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self._resolve.restype = ctypes.c_int

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


def _encode(value: str | None) -> bytes | None:
    """Marshal a hash argument to the C ABI, preserving ``None`` vs ``""``.

    ``None`` becomes ``NULL`` (unknown); a string — including the empty
    string — becomes its UTF-8 bytes. The kernel treats ``NULL`` and ``""``
    identically as "unknown", but the distinction must survive the boundary
    so the marshalling never invents information the caller did not supply.
    """
    return None if value is None else value.encode("utf-8")
