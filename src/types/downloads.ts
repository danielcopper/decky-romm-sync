/**
 * Per-ROM download types — queue entries and progress/completion events
 * for individual ROM file downloads triggered from the UI.
 */

export interface DownloadItem {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_name: string;
  status: "queued" | "downloading" | "extracting" | "completed" | "failed" | "cancelled" | "paused";
  progress: number;
  bytes_downloaded: number;
  total_bytes: number;
  /**
   * Whether the in-flight transfer can be paused and resumed. True only for
   * single-file ROMs on a direct connection where the server honoured the
   * Range probe; false for multi-file (zip) ROMs and servers behind Cloudflare.
   * The frontend offers Pause/Resume only when this is true.
   */
  resumable: boolean;
  error?: string;
}

export interface DownloadProgressEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_name: string;
  status: string;
  progress: number;
  bytes_downloaded: number;
  total_bytes: number;
  /** Server's Range-support verdict; carried live once response headers arrive. */
  resumable?: boolean;
}

export interface DownloadCompleteEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_path: string;
  /**
   * Bound Steam `app_id` for this ROM, or `null` when the ROM isn't synced
   * yet (no shortcut). Resolved on the backend so the handler confirm-sets
   * launch options on the exact shortcut without a full-library scan.
   */
  app_id: number | null;
  /** Full launch command for the just-downloaded ROM (now installed/launchable). */
  launch_options: string;
  /** Whether the just-finished transfer was resumable (carried for store parity). */
  resumable?: boolean;
  prune_lease_token?: string;
}

export interface DownloadFailedEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  error_message: string;
}

/**
 * What an entry on this device *is*, judged without following it (#260). The
 * whole vocabulary: the backend reports one of these three or reports no kind at
 * all, and a FIFO, a socket or a device node is never given one — "file or
 * folder" has no truthful answer for those, and inventing one is what let a
 * named pipe be offered as a game.
 */
export type EntryKind = "file" | "dir" | "link";

/**
 * The two sides of a download that stopped because its target path is taken
 * (#260). `sizes_match` is the comparison already made — `null` when it cannot
 * be made at all, so the dialog says so rather than implying a difference. That
 * covers a server that stated no size, and content whose byte count is not the
 * game's: a link's `size_bytes` is the length of the path it stores.
 *
 * `adoptable` is false whenever what is in the way could not become this game's
 * install: the wrong shape (a folder where the server serves one file, or the
 * reverse), a shortcut — which an uninstall can never remove — or something with
 * no `kind` at all. Replacing or cancelling are then the only honest exits.
 */
export interface TargetOccupiedResult {
  success: false;
  reason: "target_occupied";
  message: string;
  existing: {
    name: string;
    path: string;
    /**
     * What is in the way, judged without following it. `null` for something that
     * is none of the three — a FIFO, a socket, a device node — which the backend
     * reports rather than describes, because it is there and must not be written
     * over in silence.
     */
    kind: EntryKind | null;
    size_bytes: number;
    /** POSIX epoch seconds. */
    modified_at: number;
  };
  incoming: { name: string; size_bytes: number };
  sizes_match: boolean | null;
  adoptable: boolean;
}

/**
 * One entry in the platform folder that could be this game under a different
 * name (#260). `evidence` says what the offer rests on and `detail` is the whole
 * sentence stating it — ranked strongest first by the backend. Nothing here has
 * read a byte of content: `crc32` comes out of a ZIP's index and `size` out of
 * `stat`, so the strongest row is a cue to press Check Against Server, not a
 * verdict.
 */
export interface AdoptionCandidate {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes: number;
  /** POSIX epoch seconds. */
  modified_at: number;
  evidence: "crc32" | "size" | "name";
  detail: string;
}

/**
 * A download that stopped because the same game is already on disk under
 * another name. Nothing was written and no transfer started. `truncated` is
 * stated rather than implied — a list silently cut short reads as "that is all
 * there is".
 */
export interface CandidatesFoundResult {
  success: false;
  reason: "adoption_candidates";
  message: string;
  incoming: { name: string; size_bytes: number };
  candidates: AdoptionCandidate[];
  truncated: boolean;
}

/**
 * A download that stopped because something carrying this game's name cannot
 * become its install: the other shape — a folder where the server sends one
 * file, or a file where it sends a folder — or a symlink, which is never
 * adoptable whatever it points at, because an install row has to be removable
 * and the uninstall path refuses a link.
 *
 * Nothing here can be taken over, so this is not a candidate list; it is the
 * question of whether to add a second copy beside what is already there.
 * Nothing was written and no transfer started.
 *
 * Every entry here has a `kind` — the search lists nothing that has none.
 * `served_is_dir` is what the SERVER sends. `truncated` is stated rather than
 * implied, exactly as it is for the candidate list.
 */
export interface UnusableNamesakeResult {
  success: false;
  reason: "unusable_namesake";
  message: string;
  incoming: { name: string; size_bytes: number };
  existing: Array<{ name: string; path: string; kind: EntryKind }>;
  served_is_dir: boolean;
  truncated: boolean;
}

/**
 * A download that stopped because the game page reported a copy on this device
 * and the click-time search then found nothing it could name. The backstop, and
 * the reason the button's promise is keepable at all: the page and the search
 * read the same folder from different knowledge and have diverged repeatedly, so
 * this catches whatever the specific answers do not — including the ordinary
 * race where the file was deleted between opening the page and pressing.
 */
export interface CandidateVanishedResult {
  success: false;
  reason: "candidate_vanished";
  message: string;
  incoming: { name: string; size_bytes: number };
}

/** One name an adoption's rename needs that something else already holds. */
export interface RenameCollision {
  name: string;
  path: string;
  kind: "rom" | "save" | "savestate";
}

/**
 * An adoption that stopped before touching a single file because names it needs
 * are taken. Every collision is listed, because the dialog takes **one** decision
 * for the whole set: asking at the first one would mean asking with half the set
 * already moved.
 */
export interface RenameCollisionsResult {
  success: false;
  reason: "rename_collisions";
  message: string;
  collisions: RenameCollision[];
}

/** The user's one answer to the whole colliding set. */
export type CollisionChoice = "overwrite" | "keep";

/** Outcome of `adopt_existing_rom` — shaped like a completed download's bake. */
export interface AdoptResult {
  success: boolean;
  message: string;
  reason?: string;
  file_path?: string;
  rom_dir?: string | null;
  /** Bound Steam `app_id`, or `null` when the ROM has no shortcut yet. */
  app_id?: number | null;
  launch_options?: string;
  prune_lease_token?: string;
  /** Present only on a `rename_collisions` refusal. */
  collisions?: RenameCollision[];
}

/**
 * Outcome of `verify_existing_content`. A status union rather than a boolean:
 * "the server publishes no checksums" is its own answer, never a match and
 * never a mismatch.
 *
 * Each difference names one file and carries the whole sentence about it, so it
 * renders as one line. The backend owns that wording: two 32-character digests
 * said no more than "these differ" and wrapped the line into an unreadable
 * block, while a size difference states both numbers because those are numbers
 * a person can act on.
 */
export interface VerifyContentResult {
  status: "match" | "mismatch" | "unverifiable" | "missing" | "error";
  message: string;
  differences: Array<{ name: string; detail: string }>;
}

/** Byte progress of a user-requested content verification. */
export interface VerifyProgressEvent {
  rom_id: number;
  bytes_done: number;
  bytes_total: number;
}

/**
 * Per-file progress of an in-flight uninstall. Emitted only while removing a
 * multi-file ROM — a single-file removal has nothing to report between "started"
 * and "done".
 */
export interface UninstallProgressEvent {
  rom_id: number;
  files_removed: number;
  files_total: number;
}
