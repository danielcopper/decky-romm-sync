/**
 * RetroDECK file migration types — the pending-migration status, the result
 * shape returned after running a migration, and the save-sort-flag flavor
 * used when RetroArch save sorting changes. Anything that describes a
 * migration handshake between the plugin and the user lives here.
 */

export interface MigrationStatus {
  pending: boolean;
  old_path?: string;
  new_path?: string;
  roms_count?: number;
  bios_count?: number;
  saves_count?: number;
}

interface ConflictDetail {
  filename: string;
  old_path: string;
  old_size: number;
  old_mtime: string;
  new_path: string;
  new_size: number;
  new_mtime: string;
}

export interface MigrationResult {
  success: boolean;
  message: string;
  needs_confirmation?: boolean;
  conflict_count?: number;
  conflicts?: string[] | ConflictDetail[];
  roms_moved?: number;
  bios_moved?: number;
  saves_moved?: number;
  errors?: string[];
}

export interface SaveSortMigrationStatus {
  pending: boolean;
  old_settings?: { sort_by_content: boolean; sort_by_core: boolean };
  new_settings?: { sort_by_content: boolean; sort_by_core: boolean };
  saves_count?: number;
}
