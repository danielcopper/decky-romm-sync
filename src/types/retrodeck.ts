/**
 * RetroDECK path-resolution health surfaced to the QAM banner. The backend
 * resolves all RetroDECK roots (roms / saves / bios / home) from
 * `retrodeck.json`; when that read is untrustworthy the path getters fall back
 * silently, so this discriminated status lets the frontend warn the user that
 * syncs and downloads may target the wrong location. Anything describing how
 * trustworthy the resolved RetroDECK roots are lives here.
 */

/** How trustworthy the resolved RetroDECK roots are. Mirrors the backend
 *  `RetroDeckConfigHealth` StrEnum (`py_modules/lib/retrodeck_health.py`). */
export type RetroDeckHealth = "ok" | "absent" | "unreadable" | "root_missing";

/** Discriminated-status response from `get_retrodeck_status`. `config_path` is
 *  the probed `retrodeck.json`; `resolved_home` is the best-effort RetroDECK
 *  home the path getters resolved to (the fallback when the read failed). */
export interface RetroDeckStatus {
  status: RetroDeckHealth;
  config_path: string;
  resolved_home: string;
}
