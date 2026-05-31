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
}

export interface SyncStats {
  last_sync: string | null;
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
}

export interface SyncPreviewSummary {
  new_count: number;
  changed_count: number;
  unchanged_count: number;
  remove_count: number;
  disabled_platform_remove_count: number;
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
  units: SyncPlanUnit[];
  total_units: number;
  total_roms: number;
}

export interface SyncApplyUnitData {
  unit_type: "platform" | "collection";
  unit_id: number | string;
  unit_name: string;
  unit_index: number;
  total_units: number;
  shortcuts: SyncAddItem[];
}

export interface SyncStaleData {
  remove_rom_ids: number[];
}

export interface SyncCollectionsData {
  platform_app_ids: Record<string, number[]>;
  romm_collection_app_ids: Record<string, number[]>;
}
