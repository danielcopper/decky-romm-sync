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
  /**
   * Persisted post-collapse shortcut count — how many Steam shortcuts this
   * platform's ROMs collapse into (one per sibling group, ADR-0021). Absent
   * when the platform was never synced (no persisted rows); the toggle label
   * then falls back to the raw `rom_count`.
   */
  collapsed_count?: number;
  sync_enabled: boolean;
}

export type CollectionKind = "user" | "smart" | "franchise";

export type CollectionScope = "user" | "smart" | "franchise";

/**
 * QAM collection owner-scope. `"all"` (default) syncs every collection the
 * server lists; `"own"` restricts sync + display to the signed-in user's own
 * collections. Independent of the kind sub-tab — it filters by owner, not kind.
 */
export type CollectionOwnerScope = "own" | "all";

export interface CollectionSyncSetting {
  id: string;
  name: string;
  rom_count: number;
  sync_enabled: boolean;
  kind: CollectionKind;
  is_favorite: boolean;
  /**
   * Whether this collection is the signed-in user's own (#1532). Franchise
   * collections have no owner and are always `true`; when the plugin does not
   * yet know its own identity every collection is `true` (so the "Own" filter
   * degrades to "All"). Absent on older backends — treat absent as `true`.
   */
  is_own?: boolean;
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
   * Sub-phase of the ``fetching`` stage — ``"fetch"`` (paginated ROM listing)
   * or ``"covers"`` (cover download/refresh) — so the running unit's width can
   * fill each phase's own monotonic sub-slice instead of resting frozen until
   * ``applying`` (#1407). Empty/absent on every other frame (including the
   * ``fetching`` anchor and old backends), which the bar treats as "rest at the
   * unit floor" — the pre-#1407 behaviour. camelCase, matching the sibling
   * ``totalSteps`` / ``runId`` keys the backend emits on the same payload (the
   * store spreads the raw event verbatim, so the wire key IS this field name).
   */
  subStage?: string;
  /**
   * ``true`` on the frontend applying-stage frames that report cover-refresh
   * progress (``processCoverRefreshes``) rather than shortcut-item progress
   * (#1456). Frontend-only; the seed clears it at the start of every unit. Its
   * one job is to keep the live-rate ETA honest: a cover-refresh frame carries a
   * cover counter, not item progress, so the estimator must skip it exactly as it
   * skips fetch/cover frames — see the ``observeApplyProgress`` gate in
   * ``MainPage``. The counter itself surfaces only through ``message``; the bar's
   * ``current``/``total`` are left untouched (they rest at the unit's apply
   * position), so this flag has no bar effect.
   */
  coverRefresh?: boolean;
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
  /**
   * Items of the last run already done — its skipped (already-correct) entries plus
   * every committed chunk's applied shortcuts. Counted in the backend, which
   * survives the Steam restart the paused banner asks for. ``null`` when unknown
   * (no run has reached its plan in the backend process — a plugin reload wipes the
   * in-memory counters), in which case the banner omits the progress sentence
   * rather than showing a placeholder (#1383).
   */
  run_done_items: number | null;
  /** The last run's planned item total — the denominator of ``run_done_items``; ``null`` alongside it (#1383). */
  run_total_items: number | null;
}

export interface SyncStats {
  last_sync: string | null;
  /**
   * The latest run that ended in a terminal state OTHER than completed
   * (cancelled / errored / interrupted / paused), surfaced only when it is newer
   * than ``last_sync`` — so a cancelled or crash-resumed run reads as "17:48
   * (cancelled)" instead of "Never" after thousands of shortcuts were applied.
   * ``null`` (or absent) when the most recent terminal run completed cleanly.
   * Force Full Sync preserves the run history, so this display survives a reset
   * (#1318).
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
}

export interface SyncPreviewSummary {
  new_count: number;
  changed_count: number;
  unchanged_count: number;
  remove_count: number;
  disabled_platform_remove_count: number;
  /**
   * Bound ROMs whose server-side cover changed (#1386) — cover-cache refreshes
   * the apply run performs even when the shortcut delta is empty. A cover-only
   * preview (all other diffs zero, this > 0) must still offer Apply, or the
   * refresh pass never runs and the tiles stay stale. Absent on older backends
   * (treat as 0).
   */
  cover_refresh_count?: number;
  /**
   * Enabled platforms lacking a completion stamp (#1416) — a late-ack-recovered
   * platform is complete but unstamped, so its apply is a 0-delta empty final
   * chunk that re-writes the stamp and records a fresh run. A restamp-only
   * preview (all other diffs zero, this > 0) must still offer Apply, or the
   * stamp never returns and "Last sync: interrupted" lingers. Absent on older
   * backends (treat as 0).
   */
  restamp_platform_count?: number;
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

export interface SyncPlanUnit {
  type: "platform" | "collection";
  id: number | string;
  name: string;
  slug: string;
  rom_count: number;
  /** Only present when ``type === "collection"``. Discriminates user/smart/franchise. */
  collection_kind?: CollectionKind;
  /**
   * Plan-time prediction of the wholesale incremental skip (#1382) —
   * estimate-only, the fetch-time gate stays the sole skip authority
   * (ADR-0023). Present on platform units from current backends; absent on
   * collections and older backends (treat absent as "will not skip").
   */
  predicted_skip?: boolean;
  /**
   * Persisted post-collapse shortcut count for this platform (one shortcut
   * per sibling group, ADR-0021). Absent on collections, never-synced
   * platforms, and older backends; the estimate then weighs the unit by its
   * raw `rom_count`.
   */
  collapsed_count?: number;
  /**
   * This unit's known ROMs that already carry a Steam shortcut (#1511) — a
   * platform's persisted rows, or a collection's stamped member set. Those
   * items take the cheap UPDATE path in the apply loop, so the seed prices them
   * at `UPDATED_ITEM_SEC` and only the remainder at the create rate — without it
   * a re-sync (and every Force Full Sync, which unbinds nothing) is priced as a
   * fresh import. Unlike its sibling riders this rides BOTH unit kinds. Absent
   * on older backends, on never-stamped collections (a collection's membership
   * is known only from its stamp), and on franchise collections (never
   * stampable); the seed then prices every item as a create, as before.
   *
   * Note the asymmetry a Force Full Sync exposes: it clears every stamp, so its
   * PLATFORM units keep this field (read from the rows, no stamp gate) while its
   * COLLECTION units lose it and price as creates for that run.
   */
  bound_count?: number;
  /**
   * Shortcuts this platform's apply is expected to CREATE rather than update
   * (#1517) — sibling groups (ADR-0021) with no binding anywhere, unbound rows
   * with no group key, and every server ROM the local mirror holds no row for.
   * The seed uses it as the create term directly, because deriving creates by
   * subtracting `bound_count` from the unit's weight over-reads whenever that
   * weight is the pre-collapse `rom_count`: a sibling group's duplicates are
   * unbound rows that will never become shortcuts, and the subtraction prices
   * each of them as a new shortcut plus a cover download. That is precisely a
   * Force Full Sync, which drops `collapsed_count` (stamp-gated) while leaving
   * the bindings intact.
   *
   * Platform units only; absent on collections and older backends, where the
   * seed falls back to the subtraction. `0` is real knowledge, not absence — a
   * fully-mirrored platform genuinely creates nothing.
   */
  new_shortcut_count?: number;
}

export interface SyncPlanData {
  /** Identifies the sync run; captured frontend-side so a Cancel click is scoped to the active run (#1198). */
  run_id: string;
  units: SyncPlanUnit[];
  total_units: number;
  /** Raw planned ROM total (pre-collapse, skip-blind) — kept for backward compatibility. */
  total_roms: number;
  /**
   * Skip-aware estimate total (#1382): sum over units of `0` for a
   * predicted-skip unit, else `collapsed_count ?? rom_count`. Absent on older
   * backends; the seeds then fall back to `total_roms`.
   */
  total_estimated_items?: number;
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
  /**
   * EXISTING shortcuts whose server-side cover changed (#1386): the backend's
   * cover-cache invalidation pass already re-downloaded the cache and grid
   * copy; the frontend re-applies each cover via `SetCustomArtworkForApp` so
   * the Steam tile refreshes in-session (the grid file alone shows only after
   * a client restart). Rides the unit's first chunk, already clipped to the
   * session-budget headroom backend-side; empty/absent on later chunks.
   */
  cover_refreshes?: { rom_id: number; app_id: number }[];
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
