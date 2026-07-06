"""Contract test for the ``sign_out`` callable.

Driven frontend-shaped per ``src/api/backend.ts``:
``signOut = callable<[], BackendResult>("sign_out")`` — no arguments.

Pins the response SHAPE over the real ``Plugin``: signing out returns the
canonical success shape, clears the stored token locally while keeping
``romm_url`` + the SSL flag, and never deletes the token on the server. A
follow-up ``test_connection`` then reports the canonical "Not signed in"
``config_error`` because the token is gone but the URL survives.
"""

from __future__ import annotations


def _seed_signed_in(harness) -> None:
    harness.plugin.settings["romm_url"] = "http://romm.local"
    harness.plugin.settings["romm_allow_insecure_ssl"] = False
    harness.plugin.settings["romm_api_token"] = "rmm_token"
    harness.plugin.settings["romm_api_token_id"] = 42
    harness.plugin.settings["romm_api_token_origin"] = "http://romm.local"
    harness.plugin.settings["romm_api_token_source"] = "minted"


async def test_sign_out_clears_token_locally_and_keeps_url(harness):
    _seed_signed_in(harness)

    result = await harness.plugin.sign_out()

    assert result["success"] is True
    assert "Signed out" in result["message"]
    # Token trio + provenance cleared; URL + SSL flag kept.
    assert harness.plugin.settings["romm_api_token"] is None
    assert harness.plugin.settings["romm_api_token_id"] is None
    assert harness.plugin.settings["romm_api_token_origin"] is None
    assert harness.plugin.settings["romm_api_token_source"] is None
    assert harness.plugin.settings["romm_url"] == "http://romm.local"
    assert harness.plugin.settings["romm_allow_insecure_ssl"] is False
    # No server-side token deletion — sign-out is local-forget only.
    assert harness.romm.deleted_token_ids == []


async def test_test_connection_after_sign_out_is_not_signed_in(harness):
    _seed_signed_in(harness)

    await harness.plugin.sign_out()
    result = await harness.plugin.test_connection()

    assert result == {
        "success": False,
        "reason": "config_error",
        "message": "Not signed in — sign in to RomM first",
    }
