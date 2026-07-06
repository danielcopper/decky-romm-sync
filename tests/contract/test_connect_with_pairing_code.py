"""Contract test for the ``connect_with_pairing_code`` callable (pairing-code sign-in).

Driven frontend-shaped per ``src/api/backend.ts``:
``connectWithPairingCode = callable<[string, string, boolean], BackendResult>("connect_with_pairing_code")``
— positional ``(romm_url, code, allow_insecure_ssl)``.

Pins the response SHAPE over the real ``Plugin`` for the happy path (a code
exchanges for a token → success + persisted with ``"user"`` provenance and no
server-side id, exactly like a pasted token) and the failure paths (an
invalid/expired code and a rate-limit both surface the canonical
``{success: False, reason, message}`` shape with nothing persisted). The
empty-code guard is exercised too.
"""

from __future__ import annotations

from lib.errors import PairingCodeInvalidError, PairingCodeRateLimitedError


async def test_connect_with_pairing_code_happy_path_persists_user_provenance(harness):
    harness.romm.heartbeat_response = {"SYSTEM": {"VERSION": "4.9.0"}}
    harness.romm.exchange_pairing_code_response = {"id": 3, "raw_token": "rmm_paired"}

    result = await harness.plugin.connect_with_pairing_code("http://romm.local", "ABCD2345", False)

    assert result["success"] is True
    assert result["romm_version"] == "4.9.0"
    assert "Connected" in result["message"]
    # Persisted exactly like a pasted token: user provenance, no server-side id;
    # no mint, no DELETE.
    assert harness.plugin.settings["romm_api_token"] == "rmm_paired"
    assert harness.plugin.settings["romm_api_token_id"] is None
    assert harness.plugin.settings["romm_api_token_origin"] == "http://romm.local"
    assert harness.plugin.settings["romm_api_token_source"] == "user"
    assert ("mint_client_token",) not in [(c[0],) for c in harness.romm.call_log]
    assert harness.romm.deleted_token_ids == []
    # The code was normalized (uppercased) before the exchange.
    assert ("exchange_pairing_code", ("ABCD2345",), {}) in harness.romm.call_log


async def test_connect_with_pairing_code_invalid_code_returns_canonical_failure(harness):
    harness.romm.heartbeat_response = {"SYSTEM": {"VERSION": "4.9.0"}}
    harness.romm.exchange_pairing_code_side_effect = PairingCodeInvalidError("404")

    result = await harness.plugin.connect_with_pairing_code("http://romm.local", "BADCODE1", False)

    assert result["success"] is False
    assert result["reason"] == "auth_failed"
    assert "invalid or has expired" in result["message"]
    # Nothing persisted — the previous (empty) token state stands.
    assert not harness.plugin.settings.get("romm_api_token")
    assert harness.plugin.settings.get("romm_api_token_source") is None


async def test_connect_with_pairing_code_rate_limited_returns_rate_limited_reason(harness):
    harness.romm.heartbeat_response = {"SYSTEM": {"VERSION": "4.9.0"}}
    harness.romm.exchange_pairing_code_side_effect = PairingCodeRateLimitedError("429")

    result = await harness.plugin.connect_with_pairing_code("http://romm.local", "ABCD2345", False)

    assert result["success"] is False
    assert result["reason"] == "rate_limited"
    assert "Too many attempts" in result["message"]
    assert not harness.plugin.settings.get("romm_api_token")


async def test_connect_with_pairing_code_blank_code_is_config_error(harness):
    result = await harness.plugin.connect_with_pairing_code("http://romm.local", "   ", False)

    assert result == {"success": False, "reason": "config_error", "message": "Enter the pairing code from RomM"}
    # The server was never probed for a blatantly empty code.
    assert harness.romm.call_log == []
