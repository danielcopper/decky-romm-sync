"""Contract test for the ``connect_with_token`` callable (pasted API token).

Driven frontend-shaped per ``src/api/backend.ts``:
``connectWithToken = callable<[string, string, boolean], BackendResult>("connect_with_token")``
— positional ``(romm_url, token, allow_insecure_ssl)``.

Pins the response SHAPE over the real ``Plugin`` for both the happy path (a
valid token authenticates → success + persisted with ``"user"`` provenance and
no server-side id) and the failure path (a 401 from the ``/api/users/me``
validation probe → the canonical ``{success: False, reason, message}`` shape,
with nothing persisted). The empty-token guard is exercised too.
"""

from __future__ import annotations

from lib.errors import RommAuthError


async def test_connect_with_token_happy_path_persists_user_provenance(harness):
    harness.romm.heartbeat_response = {"SYSTEM": {"VERSION": "4.9.0"}}

    result = await harness.plugin.connect_with_token("http://romm.local", "rmm_pasted", False)

    assert result["success"] is True
    assert result["romm_version"] == "4.9.0"
    assert "Connected" in result["message"]
    # Persisted with user provenance and no server-side id; no mint, no DELETE.
    assert harness.plugin.settings["romm_api_token"] == "rmm_pasted"
    assert harness.plugin.settings["romm_api_token_id"] is None
    assert harness.plugin.settings["romm_api_token_origin"] == "http://romm.local"
    assert harness.plugin.settings["romm_api_token_source"] == "user"
    assert ("mint_client_token",) not in [(c[0],) for c in harness.romm.call_log]
    assert harness.romm.deleted_token_ids == []


async def test_connect_with_token_invalid_token_returns_canonical_failure(harness):
    harness.romm.heartbeat_response = {"SYSTEM": {"VERSION": "4.9.0"}}
    harness.romm.get_current_user_side_effect = RommAuthError("401")

    result = await harness.plugin.connect_with_token("http://romm.local", "rmm_bad", False)

    assert result["success"] is False
    assert result["reason"] == "auth_failed"
    assert "invalid or has been revoked" in result["message"]
    # Nothing persisted — the previous (empty) token state stands.
    assert not harness.plugin.settings.get("romm_api_token")
    assert harness.plugin.settings.get("romm_api_token_source") is None


async def test_connect_with_token_blank_token_is_config_error(harness):
    result = await harness.plugin.connect_with_token("http://romm.local", "   ", False)

    assert result == {"success": False, "reason": "config_error", "message": "Enter your RomM API token"}
    # The server was never probed for a blatantly empty token.
    assert harness.romm.call_log == []
