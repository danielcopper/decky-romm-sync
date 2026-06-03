"""In-memory ``SteamGridDbApi`` implementation for tests.

Use this fake anywhere a service needs a SteamGridDB transport in tests.
It implements every method declared on the ``SteamGridDbApi`` Protocol
in ``services.protocols.transport`` (``request``, ``download_image``,
``verify_api_key``) without any HTTP I/O.

Seed in-memory state through the typed helpers:

- ``seed_igdb_lookup(igdb_id, sgdb_id)`` — register an IGDB-to-SGDB
  match so ``request("/games/igdb/{igdb_id}")`` returns the canonical
  ``{"success": True, "data": {"id": sgdb_id}}`` payload.
- ``seed_artwork(sgdb_game_id, asset_type, image_url)`` — register an
  artwork URL so the matching ``request("/{endpoint}/game/{id}...")``
  call returns ``{"success": True, "data": [{"url": image_url}]}``.
- ``seed_raw_response(path, body)`` — register a raw JSON body for an
  arbitrary path; the fake matches by path prefix to tolerate query
  strings appended by the grid endpoint.
- ``seed_image_bytes(url, data)`` — stage bytes that ``download_image``
  will write to ``dest_path`` on success.
- ``bind_artwork_cache(cache)`` — route ``download_image`` writes into
  the bound ``FakeSgdbArtworkCache`` so ``cache.exists(dest_path)``
  flips to True without touching disk. Without a bound cache, the
  fake writes via ``pathlib`` (matching the real adapter).
- ``seed_verify_response(body)`` — set the body returned by
  ``verify_api_key`` (defaults to ``{"success": True}``).

Failure injection mirrors ``FakeRommApi``:

- ``fail_on_next(exc)`` — the next call to **any** method raises and
  the arming is consumed (one-shot).
- ``request_side_effect`` / ``download_image_side_effect`` /
  ``verify_api_key_side_effect`` — per-method exceptions that fire on
  every call until cleared.

Observability:

- ``call_log`` — ``(method_name, args, kwargs)`` per call.
- ``requested_paths`` — every path passed to ``request`` (convenience).
- ``downloaded`` — every ``(url, dest_path)`` pair passed to
  ``download_image`` (convenience).
- ``download_image_return`` — explicit return value when no
  side_effect is armed and no bytes were seeded; defaults to ``True``.
"""

from __future__ import annotations

import pathlib
from typing import Any, Protocol


class _ArtworkCacheSink(Protocol):
    """Minimal sink contract for ``download_image`` writes when bound."""

    files: dict[str, bytes]


class FakeSteamGridDbApi:
    """In-memory fake that satisfies ``SteamGridDbApi`` without HTTP."""

    def __init__(self) -> None:
        # Seeded response bodies keyed by request path (longest-prefix match).
        self._responses: dict[str, dict[str, Any] | None] = {}
        # Staged image bytes returned by ``download_image`` writes.
        self._image_bytes: dict[str, bytes] = {}
        # Optional sink that captures ``download_image`` writes when bound.
        self._artwork_cache: _ArtworkCacheSink | None = None
        # Default ``download_image`` return when no side_effect armed.
        self.download_image_return: bool = True
        # Body returned by ``verify_api_key`` when no side_effect armed.
        self.verify_response: dict[str, Any] = {"success": True}

        # Failure-injection seams.
        self._fail_on_next: Exception | None = None
        self.request_side_effect: Exception | None = None
        self.download_image_side_effect: Exception | None = None
        self.verify_api_key_side_effect: Exception | None = None

        # Observability.
        self.call_log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.requested_paths: list[str] = []
        self.downloaded: list[tuple[str, str]] = []
        self.verify_calls: list[str] = []

    # ------------------------------------------------------------------
    # Failure-injection helpers
    # ------------------------------------------------------------------

    def fail_on_next(self, exc: Exception) -> None:
        """Arm the next call (any method) to raise ``exc`` then clear the arming."""
        self._fail_on_next = exc

    def _check_fail(self, method_side_effect: Exception | None = None) -> None:
        if self._fail_on_next is not None:
            exc = self._fail_on_next
            self._fail_on_next = None
            raise exc
        if method_side_effect is not None:
            raise method_side_effect

    def _log(self, name: str, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> None:
        self.call_log.append((name, args, kwargs or {}))

    # ------------------------------------------------------------------
    # Seeding helpers
    # ------------------------------------------------------------------

    def seed_raw_response(self, path: str, body: dict[str, Any] | None) -> None:
        """Register a raw JSON body returned by ``request`` for *path*.

        Matching is by prefix so callers don't need to model the
        dimensions query string appended to ``/grids/game/{id}`` paths.
        """
        self._responses[path] = body

    def seed_igdb_lookup(self, igdb_id: int, sgdb_id: int | None) -> None:
        """Register an IGDB-to-SGDB resolution.

        Passing ``None`` for *sgdb_id* registers a "not found" response
        (``{"success": True, "data": None}``).
        """
        path = f"/games/igdb/{igdb_id}"
        if sgdb_id is None:
            self._responses[path] = {"success": True, "data": None}
        else:
            self._responses[path] = {"success": True, "data": {"id": sgdb_id}}

    def seed_artwork(self, sgdb_game_id: int, asset_type: str, image_url: str) -> None:
        """Register an artwork URL for ``request("/{endpoint}/game/{id}...")``."""
        endpoint = _ASSET_TYPE_TO_ENDPOINT.get(asset_type)
        if endpoint is None:
            raise ValueError(f"unknown asset_type: {asset_type!r}")
        path = f"/{endpoint}/game/{sgdb_game_id}"
        self._responses[path] = {"success": True, "data": [{"url": image_url}]}

    def seed_image_bytes(self, url: str, data: bytes) -> None:
        """Stage bytes that ``download_image`` writes to ``dest_path`` for *url*."""
        self._image_bytes[url] = data

    def bind_artwork_cache(self, cache: _ArtworkCacheSink) -> None:
        """Route ``download_image`` writes into *cache*.

        When bound, a successful ``download_image(url, dest_path)``
        stores the seeded bytes at ``cache.files[dest_path]`` instead of
        on disk. This lets tests assert against the in-memory
        ``FakeSgdbArtworkCache`` without standing up a real filesystem.
        """
        self._artwork_cache = cache

    def seed_verify_response(self, body: dict[str, Any]) -> None:
        """Override the body returned by ``verify_api_key``."""
        self.verify_response = body

    # ------------------------------------------------------------------
    # SteamGridDbApi Protocol surface
    # ------------------------------------------------------------------

    def request(self, path: str) -> dict[str, Any] | None:
        self._log("request", (path,))
        self.requested_paths.append(path)
        self._check_fail(self.request_side_effect)
        # Exact match first; otherwise longest-prefix match so callers
        # can seed `/grids/game/9999` and match against
        # `/grids/game/9999?dimensions=460x215,920x430`.
        if path in self._responses:
            body = self._responses[path]
        else:
            matched: str | None = None
            for seeded in self._responses:
                if path.startswith(seeded) and (matched is None or len(seeded) > len(matched)):
                    matched = seeded
            body = self._responses[matched] if matched is not None else None
        return _copy(body)

    def download_image(self, url: str, dest_path: str) -> bool:
        self._log("download_image", (url, dest_path))
        self.downloaded.append((url, dest_path))
        self._check_fail(self.download_image_side_effect)
        payload = self._image_bytes.get(url)
        if payload is not None:
            if self._artwork_cache is not None:
                self._artwork_cache.files[dest_path] = payload
            else:
                dest = pathlib.Path(dest_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
            return True
        return self.download_image_return

    def verify_api_key(self, api_key: str) -> dict[str, Any]:
        self._log("verify_api_key", (api_key,))
        self.verify_calls.append(api_key)
        self._check_fail(self.verify_api_key_side_effect)
        return dict(self.verify_response)


# Mirror the singular asset-type vocabulary owned by ``domain.sgdb_artwork``
# so tests can seed by plugin-internal name without importing the domain
# module. Kept in sync intentionally — the fake's contract is the SGDB
# HTTP path shape, not the domain mapping.
_ASSET_TYPE_TO_ENDPOINT: dict[str, str] = {
    "hero": "heroes",
    "logo": "logos",
    "grid": "grids",
    "icon": "icons",
}


def _copy(body: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a shallow copy so mutations on the returned dict don't leak back."""
    if body is None:
        return None
    return dict(body)
