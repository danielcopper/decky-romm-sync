import { toaster } from "@decky/api";
import type { ToastData, ToastNotification } from "@decky/api";
import type { ReactNode } from "react";

// Must match `plugin.json`'s `name` and the `name` returned from `definePlugin`
// — Decky reads those two for the plugin list and the QAM header, and nothing
// checks that the three agree.
export const PLUGIN_NAME = "RomM Sync";

// Save-sync notices carry their own title so the user can tell an automatic
// save operation from everything else the plugin reports.
export const SAVE_SYNC_TOAST_TITLE = "RomM Save Sync";

/**
 * Raise a toast under the plugin's name.
 *
 * *options* passes through to `toaster.toast` and may override the title —
 * save-sync call sites do, via {@link SAVE_SYNC_TOAST_TITLE}.
 */
export function showToast(body: ReactNode, options?: Partial<Omit<ToastData, "body">>): ToastNotification {
  return toaster.toast({ title: PLUGIN_NAME, body, ...options });
}
