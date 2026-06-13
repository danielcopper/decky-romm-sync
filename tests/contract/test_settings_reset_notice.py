"""Contract test for the corrupt-settings-reset notice callable.

Driven frontend-shaped per ``src/api/backend.ts``:
``consumeSettingsResetNotice = callable<[], {reset: boolean; backed_up_to: string | null}>``.

The corruption path itself is hard to drive cleanly through the real
bootstrap (it writes fresh defaults over a quarantined file), so the
adapter unit tests are the primary coverage. This pins the clean-boot
shape over the real Plugin: a non-corrupt boot returns ``reset: False``
with ``backed_up_to: None`` (literal None → JS null).
"""

from __future__ import annotations


async def test_consume_settings_reset_notice_clean_boot_shape(harness):
    result = await harness.plugin.consume_settings_reset_notice()
    assert result == {"reset": False, "backed_up_to": None}
    assert result["backed_up_to"] is None
