/**
 * Library-sync types — platforms, collections, preview/plan/apply payloads,
 * and the sync-progress UI state. Anything related to the bulk
 * RomM→Steam shortcut sync flow lives here.
 */

export interface PlatformSyncSetting {
  id: number;
  name: string;
  slug: string;
  rom_count: number;
  sync_enabled: boolean;
}

export type CollectionKind = "user" | "smart" | "franchise";

export type CollectionScope = "my" | "smart" | "franchise";

export interface CollectionSyncSetting {
  id: string;
  name: string;
  rom_count: number;
  sync_enabled: boolean;
  kind: CollectionKind;
  is_favorite: boolean;
}

export type SyncStage = "discovering" | "fetching" | "applying" | "finalizing" | "done" | "cancelled" | "error";

export interface SyncProgress {
  running: boolean;
  stage?: SyncStage | "";
  /** Fine: items processed within the current unit. */
  current?: number;
  /** Fine: total items in the current unit. */
  total?: number;
  message?: string;
  /** Coarse: current unit index (1-based) driving the determinate main bar. */
  step?: number;
  /** Coarse: total units. ``0`` means indeterminate. */
  totalSteps?: number;
  /**
   * Backend run identity for the in-flight sync, stamped from the backend's
   * ``current_sync_id``. ``""`` when no run is in flight. The authoritative
   * source a Cancel click scopes itself to — the frontend no longer mirrors a
   * separate run id (#1202).
   */
  runId?: string;
  /**
   * Frontend-computed upper-bound apply duration (seconds) for the in-flight
   * run, derived once from the ``sync_plan`` payload's ``total_roms`` — an
   * honest ceiling (every ROM priced as new) that the applying UI surfaces as
   * "up to ~X min". Never sent by the backend; set by the ``sync_plan``
   * listener and preserved across backend ``sync_progress`` frames.
   */
  etaSeconds?: number;
}

export interface SessionBudgetStatus {
  success: boolean;
  /**
   * Live renderer RSS in KB, or ``null`` when unreadable (no ``steamwebhelper`` /
   * unreadable ``/proc``). The banners drop the number but keep their text when
   * this is ``null`` (#1383).
   */
  rss_kb: number | null;
  /**
   * The advisory floor in KB (~1.8 GB) — strictly above this the value colours
   * yellow (and the yellow high-heap banner appears). Backend-supplied so the
   * frontend holds no threshold magic numbers (#1383).
   */
  warn_kb: number;
  /** The effective pause ceiling in KB (~2.2 GB) — a chunk projected past this pauses; value colours red at/above it. */
  ceiling_kb: number;
  /** The measured OOM cliff in KB (~2.45 GB) the renderer crashes at. */
  cliff_kb: number;
  /**
   * Signed renderer-RSS growth (KB) of the last run (end − start), measured at
   * EVERY terminal — completed, paused, cancelled, or interrupted — so the row
   * reflects that run's consumption, not a prior clean run's. Retained in backend
   * memory so a QAM remount can show "last run: ±X GB" without a live run. ``null``
   * when either endpoint was unmeasurable (or after a plugin reload). Rendered
   * sign-formatted (#1383 / #36).
   */
  memory_delta_kb: number | null;
  /**
   * Whether resuming a paused run now would apply at least one full chunk without
   * re-pausing — the gate's own predictive condition against the live reading. Once
   * a Steam restart drops RSS this flips ``true`` and the paused banner tells the
   * user memory is free again (and hides the restart button). ``null`` when the
   * reading is unavailable (undecidable → conservative fail-open). (#1383)
   */
  resume_ready: boolean | null;
}

export interface SyncStats {
  last_sync: string | null;
  /**
   * The latest run that ended in a terminal state OTHER than completed
   * (cancelled / errored), surfaced only when it is newer than ``last_sync`` —
   * so a cancelled or crash-resumed run reads as "17:48 (cancelled)" instead of
   * "Never" after thousands of shortcuts were applied. ``null`` (or absent) when
   * the most recent terminal run completed cleanly.
   */
  last_attempt?: { finished_at: string; status: "cancelled" | "errored" | "interrupted" | "paused" } | null;
  platforms: number;
  collections?: number;
  roms: number;
  total_shortcuts: number;
}

export interface RegistryPlatform {
  name: string;
  slug: string;
  count: number;
}

export interface SyncAddItem {
  rom_id: number;
  name: string;
  exe: string;
  start_dir: string;
  launch_options: string;
  platform_name: string;
  cover_path: string;
  /**
   * Present only on a sibling-group REBIND entry (ADR-0021 §2): the entry is
   * keyed to the vanished bound sibling's `rom_id` so the frontend reuses its
   * existing shortcut, while the backend moves the DB binding onto this
   * representative at commit. Shortcut reuse is still keyed by `rom_id` through
   * the existing-shortcut map, but the frontend DOES read this to fetch the
   * representative's artwork (covers can be language-/edition-specific, so the
   * shortcut must show the bound version's art, not the vanished sibling's). A
   * plain optional number, JSON-safe.
   */
  bind_rom_id?: number;
}

export interface SyncPreviewSummary {
  new_count: number;
  changed_count: number;
  unchanged_count: number;
  remove_count: number;
  disabled_platform_remove_count: number;
  /** Scope of the run — how many platforms this sync spans (always shown, independent of diffs). */
  sync_platform_count?: number;
  /** Scope of the run — how many collections this sync spans. */
  sync_collection_count?: number;
  collection_diff?: {
    has_changes: boolean;
    added: string[];
    removed: string[];
  };
  platform_collection_diff?: {
    has_changes: boolean;
    added_count: number;
    removed_count: number;
  };
}

export interface SyncPreview {
  success: boolean;
  summary: SyncPreviewSummary;
  new_names: string[];
  changed_names: string[];
  preview_id: string;
  message?: string;
  blocked_by_migration?: boolean;
  /**
   * Post-preview session-budget prognosis (#1383): ``true`` when the backend
   * predicts that applying every planned touch would push Steam's renderer past
   * its per-session heap budget, so the sync will likely pause partway (and can
   * always be resumed). Drives the yellow advisory hint on the preview. Absent /
   * ``false`` when the reading is unavailable or the run fits under the budget.
   */
  pause_likely?: boolean;
}

interface SyncPlanUnit {
  type: "platform" | "collection";
  id: number | string;
  name: string;
  slug: string;
  rom_count: number;
  /** Only present when ``type === "collection"``. Discriminates user/smart/franchise. */
  collection_kind?: CollectionKind;
}

export interface SyncPlanData {
  /** Identifies the sync run; captured frontend-side so a Cancel click is scoped to the active run (#1198). */
  run_id: string;
  units: SyncPlanUnit[];
  total_units: number;
  total_roms: number;
}

export interface SyncApplyUnitData {
  /** Identifies the sync run; keys the frontend's once-per-run shortcut-scan cache. */
  run_id: string;
  unit_type: "platform" | "collection";
  unit_id: number | string;
  unit_name: string;
  unit_index: number;
  total_units: number;
  /**
   * A unit's shortcuts are emitted in chunks, each acked + committed durably
   * before the next, so a mid-unit CEF crash forfeits only the in-flight chunk.
   * ``chunk_index`` (0-based) is echoed back in the ack so the backend rejects a
   * stale chunk; ``chunk_offset`` / ``unit_total`` drive unit-wide progress that
   * stays continuous across chunks; ``shortcuts`` is this chunk's slice.
   */
  chunk_index: number;
  chunk_count: number;
  chunk_offset: number;
  unit_total: number;
  shortcuts: SyncAddItem[];
}

export interface SyncStaleData {
  /**
   * Bound stale ROMs to remove from Steam. Each entry carries the `app_id`
   * read on the backend BEFORE the row was unbound, so the handler removes
   * the shortcut directly without re-resolving rom_id→app_id (which races
   * the backend unbind). Unbound stale ROMs are excluded — they have no
   * Steam shortcut to remove.
   */
  remove: { rom_id: number; app_id: number }[];
}

export interface SyncCollectionsData {
  platform_app_ids: Record<string, number[]>;
  romm_collection_app_ids: Record<string, number[]>;
}
