"""Contract tests for ``get_pending_preview`` — the preview a remounted panel asks for.

The QAM panel loses its preview card whenever the user navigates into a
submenu, while the backend goes on holding the staged snapshot for its full
TTL. This callable is how the panel gets the card back, so what it returns has
to be exactly the payload ``sync_preview`` answered with — including the
absolute ``expires_at`` deadline the card counts down against — and "nothing
pending" has to read as a normal answer rather than a failure.

Driven through the real callables over the real wired plugin.
"""

from __future__ import annotations


def _seed_one_platform(harness):
    harness.romm.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
    harness.romm.roms[10] = {
        "id": 10,
        "name": "Game",
        "platform_id": 1,
        "platform_name": "N64",
        "platform_slug": "n64",
    }
    harness.plugin.settings["enabled_platforms"] = {"1": True}


async def test_nothing_pending_is_a_success_not_a_failure(harness):
    """No preview staged is a normal answer — the failure shape is for failures."""
    assert await harness.plugin.get_pending_preview() == {"success": True, "preview": None}


async def test_restores_the_payload_sync_preview_answered_with(harness):
    _seed_one_platform(harness)

    fresh = await harness.plugin.sync_preview()
    restored = await harness.plugin.get_pending_preview()

    assert fresh["success"] is True
    assert restored == {"success": True, "preview": fresh}
    # The restored card counts down against the same absolute deadline the
    # fresh one did, so both render identically.
    preview = restored["preview"]
    assert preview is not None
    assert preview["expires_at"] == fresh["expires_at"]


async def test_restored_preview_id_still_applies(harness):
    """The point of restoring the card: its Apply Sync must still be honoured."""
    _seed_one_platform(harness)
    await harness.plugin.sync_preview()

    restored = await harness.plugin.get_pending_preview()
    preview = restored["preview"]
    assert preview is not None

    applied = await harness.plugin.sync_apply_delta(preview["preview_id"])
    assert applied == {"success": True, "message": "Applying changes"}


async def test_withheld_while_a_run_is_in_flight_then_handed_back(harness):
    """A run in flight owns the panel: the callable answers "nothing pending"
    rather than a card the frontend would render over the run's progress rows.

    The frontend cannot be the authority here — an apply refused with
    ``sync_in_progress`` retracts its optimistic running flag while the backend
    deliberately keeps the delta (#1202), so its store says idle during a live
    run. Withheld, not discarded: once the run ends the same payload comes back.
    """
    _seed_one_platform(harness)
    fresh = await harness.plugin.sync_preview()
    box = harness.plugin._sync_service._box
    assert box.try_begin_run("run-1") is True

    assert await harness.plugin.get_pending_preview() == {"success": True, "preview": None}
    assert box.pending_delta is not None

    # An overlapping apply keeps its own refusal — the withholding is the
    # restore reader's alone and must not leak into the apply's verdict.
    rejected = await harness.plugin.sync_apply_delta(fresh["preview_id"])
    assert rejected == {"success": False, "reason": "sync_in_progress", "message": "Sync already in progress"}

    box.finish_run("run-1")
    assert await harness.plugin.get_pending_preview() == {"success": True, "preview": fresh}


async def test_over_age_snapshot_is_dropped(harness):
    """Past the TTL the read answers "nothing pending" and clears the staged
    snapshot, on the same rule the apply refuses it by."""
    _seed_one_platform(harness)
    await harness.plugin.sync_preview()
    box = harness.plugin._sync_service._box
    assert box.pending_delta is not None

    harness.plugin._sync_service._orchestrator._clock.advance(1801)

    assert await harness.plugin.get_pending_preview() == {"success": True, "preview": None}
    assert box.pending_delta is None
