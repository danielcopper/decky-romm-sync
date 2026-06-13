"""Canonical server-origin derivation and comparison for server-identity binding.

A stored Client API Token is minted against one RomM server and must never be
replayed against another. The comparison key is the **full origin** — scheme,
host, and (non-default) port — so this module turns an arbitrary user-entered
URL into that canonical key and answers whether two URLs name the same server.
``https://h`` and ``http://h`` are deliberately distinct origins: a downgrade to
plaintext is a different (and hostile) destination, not the same one.

Anything that needs to decide "is this URL the same server as that one?" or
"is this a usable http(s) server URL?" belongs here. Stdlib-only (``urllib``);
no I/O, no network — purely lexical normalization.
"""

from __future__ import annotations

import urllib.parse

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(url: str) -> str | None:
    """Return the canonical ``scheme://host[:port]`` origin of *url*, or ``None``.

    Scheme and host are lowercased; the default port for the scheme (``443``
    for https, ``80`` for http) is folded out; any path, query, fragment, or
    trailing slash is dropped. An IPv6 literal keeps its brackets
    (``https://[::1]:8080``) and a trailing FQDN root dot is normalized away
    (``host.com.`` → ``host.com``), so the result always re-parses through this
    function unchanged. Returns ``None`` when *url* has no scheme, no host, or a
    scheme other than ``http`` / ``https`` — i.e. it is not a usable server
    origin and cannot serve as a comparison key.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    # urlsplit().hostname strips IPv6 brackets and never trims the FQDN root dot.
    # Re-bracket an IPv6 literal (any host containing ``:``) so the origin
    # re-parses, and drop a single trailing dot so ``host.com.`` == ``host.com``.
    host = host.rstrip(".") or host
    host_part = f"[{host}]" if ":" in host else host
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host_part}"
    return f"{scheme}://{host_part}:{port}"


def same_origin(a: str | None, b: str | None) -> bool:
    """Return True iff *a* and *b* normalize to the same non-``None`` origin.

    A ``None`` (or otherwise unparseable) origin on either side is never equal
    to anything — fail closed: an unknown origin is not "the same server".
    """
    origin_a = normalize_origin(a) if a is not None else None
    origin_b = normalize_origin(b) if b is not None else None
    return origin_a is not None and origin_a == origin_b


def is_valid_server_url(url: str) -> bool:
    """Return True iff *url* (after stripping surrounding whitespace) names an http(s) origin.

    The validation guard for user-entered server URLs: a value is valid when it
    carries an ``http`` / ``https`` scheme and a host. Path / port / query are
    allowed (they are dropped by :func:`normalize_origin`); a scheme-less,
    hostless, or non-http(s) value is rejected.
    """
    return normalize_origin(url.strip()) is not None
