"""Pure decoding of the ``platform_slug → display_name`` cache.

The cache is a JSON object stored as a single ``kv_config`` TEXT value, refreshed
each sync so offline reads (DangerZone labels, game-detail platform names) show a
human platform name rather than the bare slug. Anything that turns the stored
string back into the ``slug → name`` mapping belongs here; reading the value out
of ``kv_config`` stays in the service.
"""

from __future__ import annotations

import json


def decode_platform_names(raw: str | None) -> dict[str, str]:
    """Decode the cached ``platform_slug → display_name`` mapping.

    Returns ``{}`` when *raw* is absent, empty, not valid JSON, or decodes to
    anything other than an object — callers degrade to the slug in every such
    case rather than surfacing a corrupt cache.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
