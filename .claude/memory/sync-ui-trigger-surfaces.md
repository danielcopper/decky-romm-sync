---
name: sync-ui-trigger-surfaces
description: Which UI surface triggers which save-sync backend call, and which one actually surfaces the SyncConflictModal. Three surfaces exist (per-ROM Sync button, game-detail page open, Play button) — only the Play button (CustomPlayButton) opens the conflict modal; the others just toast or refresh. When smoke-testing the conflict modal, use the Play button in its conflict state — the Sync button looks like the obvious entry point but isn't.
type: project
---

# Sync UI triggers — which surface does what

Three user-facing surfaces can trigger save sync. They are NOT interchangeable for testing the conflict modal:

| Surface                                     | Backend call              | Surfaces conflict modal?                                                                                               |
| ------------------------------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Per-ROM **Sync** button on game-detail page | `sync_rom_saves(rom_id)`  | **No.** Just toasts the result.                                                                                        |
| Game-detail page **open** (component mount) | `get_save_status(rom_id)` | **No.** Just refreshes the SAVES tab.                                                                                  |
| **Play** button (`CustomPlayButton`)        | `pre_launch_sync(rom_id)` | **Yes.** Button switches to "Resolve conflict" state when a conflict is pending; tapping it opens `SyncConflictModal`. |

So: when smoke-testing or debugging the conflict modal, the only way to surface it is via the Play button (in its
conflict-state form). The Sync button and page-open run sync but never open the modal.

This is non-obvious — the Sync button looks like it might surface conflicts because it's the explicit "sync now" action.
It doesn't.
