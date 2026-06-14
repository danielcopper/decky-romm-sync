"""Contract test for the persistent corrupt-settings-reset notice callables.

Driven frontend-shaped per ``src/api/backend.ts``:
``getSettingsResetNotice = callable<[], {pending: boolean; backed_up_to: string | null}>``
and ``dismissSettingsResetNotice = callable<[], {success: boolean}>``.

The corruption path itself is hard to drive cleanly through the real
bootstrap (it needs a pre-existing unparseable settings.json before the
composition root runs) — the bootstrap unit test and the main.py callable
unit tests are the primary coverage. This pins the callables' response shapes
over the real Plugin:

* clean boot → ``pending: False`` with ``backed_up_to: None`` (literal None → JS null);
* marker present → ``pending: True`` with the backup filename, and the read is
  NON-consuming (a second read still reports pending);
* an explicit ack (``dismiss_settings_reset_notice``) clears the marker so the
  next read reports not-pending.
"""

from __future__ import annotations


async def test_get_settings_reset_notice_clean_boot_shape(harness):
    result = await harness.plugin.get_settings_reset_notice()
    assert result == {"pending": False, "backed_up_to": None}
    assert result["backed_up_to"] is None


async def test_get_settings_reset_notice_pending_is_non_consuming(harness):
    # Simulate bootstrap having folded a corrupt-reset into the live settings.
    harness.plugin.settings["_settings_reset_notice"] = {"backed_up_to": "settings.json.corrupt-1781697600"}

    first = await harness.plugin.get_settings_reset_notice()
    assert first == {"pending": True, "backed_up_to": "settings.json.corrupt-1781697600"}

    # Non-consuming: a second read still reports pending (the marker is cleared
    # only by an explicit ack, not by reading it).
    second = await harness.plugin.get_settings_reset_notice()
    assert second == first


async def test_dismiss_settings_reset_notice_clears_marker(harness):
    harness.plugin.settings["_settings_reset_notice"] = {"backed_up_to": "settings.json.corrupt-42"}

    ack = await harness.plugin.dismiss_settings_reset_notice()
    assert ack == {"success": True}

    # The marker is gone and the read now reports not-pending.
    assert "_settings_reset_notice" not in harness.plugin.settings
    assert await harness.plugin.get_settings_reset_notice() == {"pending": False, "backed_up_to": None}
