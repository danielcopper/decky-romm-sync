"""Path containment predicates for guarding destructive filesystem ops.

Stateless safety checks that answer "is path X safely inside configured
root Y?". Uses ``os.path.realpath`` to resolve symlinks before comparing,
so callers cannot escape the configured root via a symlink — which means
this is not pure compute (``realpath`` is an ``lstat`` syscall) and
belongs in ``lib/`` rather than ``domain/``. The source of the
configured root (e.g. the ``RetroDeckPaths`` Protocol) stays in
``services/``; this module only consumes the resolved string.

Two guards live here for server-supplied path components:

- :func:`safe_path_component` — lexical (no I/O). Validates that an
  untrusted string is a single safe filename component before it is
  joined onto a base.
- :func:`safe_join` — realpath containment. Joins untrusted parts onto a
  trusted base and confirms the result resolves strictly below the base
  (the same semantics as :func:`is_safe_rom_path`), so a multi-component
  registry path or a symlink cannot escape.

Both raise :class:`PathTraversalError` on a violation so call sites can
fail closed (abort the operation, surface a canonical failure) rather
than silently writing outside the configured root.
"""

from __future__ import annotations

import os


class PathTraversalError(ValueError):
    """Raised when an untrusted path component escapes its base directory.

    A :class:`ValueError` subclass so existing ``except ValueError`` write
    paths still catch it, while call sites that want the distinct
    ``path_traversal`` failure slug can catch this type first.
    """


def safe_path_component(name: str) -> str:
    """Validate that *name* is a single safe filesystem component. Lexical, no I/O.

    A defence for server-supplied names that must stay a single component
    (e.g. a decoded ZIP member basename). Unlike
    ``domain.save_path.sanitize_save_filename`` — which silently reduces a
    name to its basename — this rejects anything that is not already a
    clean single component, so a traversal attempt aborts the operation
    rather than being quietly defanged.

    Rejects (raising :class:`PathTraversalError`):

    - a NUL byte anywhere in *name*
    - the empty string, ``"."`` or ``".."``
    - an absolute path
    - any value containing a path separator, or whose ``os.path.normpath``
      introduces or retains a ``..`` segment or separator (a single
      component must normalize to itself)

    Returns *name* unchanged when it is already a valid single component.
    """
    if "\x00" in name:
        raise PathTraversalError("path component contains a NUL byte")
    if name in ("", ".", ".."):
        raise PathTraversalError(f"not a valid path component: {name!r}")
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        raise PathTraversalError(f"path component must not contain a separator: {name!r}")
    # normpath collapses ``a/../b`` → ``b`` and ``./x`` → ``x``; a clean
    # single component is a fixed point. Anything that changes — or that
    # normalizes to a ``..`` escape — is rejected.
    if os.path.normpath(name) != name:
        raise PathTraversalError(f"path component is not normalized: {name!r}")
    return name


def safe_join(base: str, *parts: str) -> str:
    """Join *parts* onto *base* and return the realpath, or raise on escape.

    Realpath-containment guard for server-supplied path components: the
    joined path must resolve **strictly below** ``base`` — equality with
    the base is rejected, matching :func:`is_safe_rom_path`. Symlinks are
    resolved (``realpath``), so a symlink planted under ``base`` that
    points outside cannot be used to escape.

    Legitimate multi-component parts (e.g. the registry ``dc/dc_boot.bin``)
    are allowed as long as the resolved path stays under ``base``. Raises
    :class:`PathTraversalError` otherwise; returns the resolved absolute
    path on success.
    """
    real_base = os.path.realpath(base)
    real_joined = os.path.realpath(os.path.join(base, *parts))
    if not real_joined.startswith(real_base + os.sep):
        raise PathTraversalError(f"path escapes base directory: {os.path.join(base, *parts)!r} not under {base!r}")
    return real_joined


def is_safe_rom_path(path: str, roms_base: str) -> bool:
    """Return True when ``path`` is safely inside ``roms_base``.

    Two properties must hold:

    1. ``os.path.realpath(path)`` lies strictly inside
       ``os.path.realpath(roms_base) + os.sep`` — equality with the base
       is rejected and symlinks escaping the base are rejected.
    2. The resolved path is at least two segments below the base, so a
       bare platform directory (e.g. ``roms_base/gb/``) does not qualify
       while a file beneath one (e.g. ``roms_base/gb/file.zip``) does.
    """
    resolved = os.path.realpath(path)
    real_base = os.path.realpath(roms_base)
    if not resolved.startswith(real_base + os.sep):
        return False
    rel = os.path.relpath(resolved, real_base)
    parts = rel.split(os.sep)
    return len(parts) >= 2
