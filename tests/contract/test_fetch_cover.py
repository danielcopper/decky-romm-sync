"""Contract tests for ``fetch_cover_base64`` over the real Plugin/bootstrap.

Driven frontend-shaped per ``src/api/backend.ts``:
``fetchCoverBase64 = callable<[number], { base64: string | null }>`` — a single
positional rom_id, asserting the literal ``{"base64": ...}`` data shape. It is a
data callable, not a ``{success, reason, message}`` result: every failure —
server unreachable, a ROM without a cover — is a silent ``{"base64": None}``.

Only the RomM transport is faked; the per-ROM cover cache is the REAL
CoverArtFileStore adapter writing under the harness ``tmp_path`` runtime dir.
"""

from __future__ import annotations

import base64


async def test_fetch_cover_base64_downloads_and_returns_bytes(harness):
    """A cache miss fetches the ROM's cover from RomM and returns its bytes."""
    harness.romm.roms[7] = {"id": 7, "path_cover_large": "/cover/7.png"}
    harness.romm.download_payloads["cover:/cover/7.png"] = b"PNGBYTES"

    result = await harness.plugin.fetch_cover_base64(7)
    assert result == {"base64": base64.b64encode(b"PNGBYTES").decode("ascii")}


async def test_fetch_cover_base64_cache_hit_is_served_without_redownload(harness):
    """A second call is served from the cache — no re-download."""
    harness.romm.roms[7] = {"id": 7, "path_cover_large": "/cover/7.png"}
    harness.romm.download_payloads["cover:/cover/7.png"] = b"ORIGINAL"

    first = await harness.plugin.fetch_cover_base64(7)
    assert base64.b64decode(first["base64"]) == b"ORIGINAL"

    # Change the server payload; a cache hit must NOT re-download it.
    harness.romm.download_payloads["cover:/cover/7.png"] = b"CHANGED"
    second = await harness.plugin.fetch_cover_base64(7)
    assert base64.b64decode(second["base64"]) == b"ORIGINAL"


async def test_fetch_cover_base64_works_without_local_db_row(harness):
    """A server-only version (no ``roms`` row) still resolves its cover."""
    harness.romm.roms[123] = {"id": 123, "path_cover_small": "/cover/small.png"}
    harness.romm.download_payloads["cover:/cover/small.png"] = b"SMALL"

    result = await harness.plugin.fetch_cover_base64(123)
    assert base64.b64decode(result["base64"]) == b"SMALL"


async def test_fetch_cover_base64_server_unreachable_returns_null(harness):
    """A transport failure degrades silently to ``{"base64": None}``."""
    harness.romm.get_rom_side_effect = ConnectionError("down")

    result = await harness.plugin.fetch_cover_base64(7)
    assert result == {"base64": None}
    assert "success" not in result and "reason" not in result


async def test_fetch_cover_base64_rom_without_cover_returns_null(harness):
    """A ROM with no cover URL returns ``{"base64": None}`` silently."""
    harness.romm.roms[7] = {"id": 7, "name": "No Cover"}

    result = await harness.plugin.fetch_cover_base64(7)
    assert result == {"base64": None}
