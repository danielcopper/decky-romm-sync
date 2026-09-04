/**
 * Firmware and BIOS types — server-side firmware inventory, local BIOS
 * file status, and the available-cores selection presented in the UI.
 */

/**
 * What the machine answers about one firmware file, in the four values the
 * resolver's per-file answer and the reading's completeness together produce.
 * `not_needed` is a finished answer — every emulator the platform offers was
 * read and none asks for the file — while `unknown` is the absence of one. They
 * are never folded together: the collapse is what made a file the plugin could
 * not ask about look like a file nothing wanted.
 */
export type FirmwareWanted = "needed" | "optional" | "not_needed" | "unknown";

/**
 * What the emulator opens a firmware declaration AT — a file it reads, or a
 * folder it lists. A property of the DECLARATION, so it survives an empty
 * destination: a folder that is not there is still a folder to create, never a
 * file to fetch, which is why no download affordance may offer such a row.
 */
export type FirmwareDeclaredKind = "file" | "directory";

/**
 * The reading's answer about one row, carried on both row shapes.
 *
 * `satisfied` is the verdict and the axis the REQUIRED counts key off: the
 * requirement is met, is not met, or nothing established which. It is not
 * `downloaded` — for a folder declaration the two come apart completely, since
 * what satisfies the core is a file *inside* the folder and RetroDECK links
 * LRPS2's `pcsx2/bios` onto the BIOS root, so the folder is always there. The
 * library's held/offered ratio is a third axis and keys off neither.
 *
 * `caveats` are the resolver's own stable codes for what it found, and `images`
 * names what a satisfied folder holds, in the resolver's own words. A surface
 * takes the CAUSE of a verdict from those, because `satisfied` is deliberately
 * the verdict alone and carries none of it.
 */
interface FirmwareVerdict {
  satisfied?: boolean | null;
  declared_kind?: FirmwareDeclaredKind;
  caveats?: string[];
  images?: string[];
}

interface FirmwareFile extends FirmwareVerdict {
  /** `null` for a file an installed emulator asks for that the RomM library does
   *  not hold — there is no server record to name. Nothing reads it: what the
   *  page filters the download buttons and its progress totals on is
   *  `on_server`. */
  id: number | null;
  file_name: string;
  size: number;
  md5: string;
  local_path: string;
  /** Where the emulator said the file goes, relative to the firmware root —
   *  `dc/dc_boot.bin` where a subdirectory was declared, the bare name
   *  otherwise. `file_name` is its basename and `local_path` is it joined under
   *  a root this side does not know, so this is the only field that can answer
   *  which folder a file placed by hand belongs in. Optional because a payload
   *  from before the field existed carries none. */
  declared_path?: string;
  downloaded: boolean;
  description: string;
  wanted: FirmwareWanted;
  /** Whether the platform's launching core is the one that requires it — the
   *  axis the "BIOS needed" badge and the required counts key off, distinct
   *  from `wanted`, which is about every installed emulator. */
  required_by_active: boolean;
  on_server: boolean;
  supplied_by?: string | null;
}

interface FirmwarePlatform {
  platform_slug: string;
  files: FirmwareFile[];
}

/**
 * One ES-DE `<command>` classified for launch-bakeability (#1210), the shape the
 * backend `get_emulator_options` / picker payloads carry. `kind` is `"libretro"`
 * (`core_so` set to the bare core name) or `"standalone"` (`core_so` null).
 * `bakeable` is false for the `needs_setup` (`reason: "inject"`) and un-bakeable
 * forms; `reason` names why the frontend can't offer it as a clickable pick.
 */
export interface EmulatorOption {
  label: string;
  kind: "libretro" | "standalone";
  core_so: string | null;
  is_default: boolean;
  bakeable: boolean;
  reason: string | null;
}

/**
 * Response shape of the `get_platform_core_info` callable — the dedicated
 * single-platform emulator-info path, decoupled from the per-game BIOS payload
 * (#923). The per-game detail page (`RomMPlaySection` / `RomMGameInfoPanel`)
 * reads emulator data from here; the platform-keyed twin for a caller with no
 * ROM to layer is {@link SystemCoreInfo}. `emulator_data_available` is false
 * when `es_systems.xml` cannot be read (RetroDECK not detected), so the picker
 * can say so instead of an empty list.
 */
export interface CoreInfo {
  emulators: EmulatorOption[];
  emulator_data_available: boolean;
  active_core: string | null;
  active_core_label: string | null;
  platform_core_label: string | null;
  has_game_override: boolean;
}

/**
 * Response shape of the `get_system_core_info` callable — the platform-keyed
 * core read the Library page's Platforms detail issues once per selected
 * platform (#1815). Carries what the core picker needs and nothing else: no
 * per-game layer (there is no ROM), and no BIOS data (the detail's table reads
 * that off `get_firmware_status`).
 *
 * `active_core_label` is the platform-layer resolution — the per-platform
 * override when it still resolves to a bakeable emulator, else the es_systems
 * default — so in both ordinary cases it IS the emulator's name. `null` means
 * no option is BAKEABLE, which is not the same as there being none: a menu of
 * uninstalled standalone emulators answers `null` with `emulators.length >= 1`.
 * RetroDECK then resolves the emulator itself at launch, and the Library page
 * says that rather than printing a name or claiming a failure.
 *
 * There is no `success`, for the reason its sibling has none: the callable has
 * no in-band failure to report. What can go wrong — an unreadable
 * `es_systems.xml` — is already `emulator_data_available: false`, and anything
 * else raises, which reaches the caller as a rejected promise.
 */
export interface SystemCoreInfo {
  emulators: EmulatorOption[];
  emulator_data_available: boolean;
  active_core_label: string | null;
}

/**
 * Per-platform entry in the `get_firmware_status` overview: the BIOS file state
 * of every platform the payload can speak for, in one call. This is the
 * library-wide overview path — distinct from the per-game `check_platform_bios`
 * payload, which no longer carries any core fields (#923). Its core fields are
 * a second answer to {@link SystemCoreInfo}'s question, kept because they cost
 * nothing extra here; a platform this payload has nothing to say about carries
 * no entry at all, which is why the Platforms detail asks the core read
 * directly rather than joining onto this one.
 *
 * `files` is the union of what the RomM library offers for the platform and what
 * the platform's emulators ask for, so a row can be present with `on_server`
 * false — wanted, possibly missing, and not downloadable from here.
 */
export interface FirmwarePlatformExt extends FirmwarePlatform {
  has_games?: boolean;
  all_downloaded?: boolean;
  active_core?: string;
  active_core_label?: string;
  emulators?: EmulatorOption[];
  emulator_data_available?: boolean;
  // Per-platform BIOS aggregates computed by the backend from the same
  // core-aware classified files (`compute_bios_level`), so the platform detail
  // reads the unknown/ok/partial/missing decision and display counts off the
  // payload instead of re-deriving the threshold logic. The optional-missing
  // breakdown stays a local file-level computation (a richer axis the level
  // doesn't model).
  bios_level?: BiosLevel | null;
  required_count?: number;
  required_downloaded?: number;
  /** How many of `required_count` nothing could judge — see `BiosStatus`. Above
   *  zero it is why `bios_level` is `"unknown"`, and it is what separates that
   *  from a platform nothing could speak for: here the rows have answers and
   *  only the one-line verdict declines, so the downloads stay. */
  required_withheld?: number;
  server_count?: number;
  local_count?: number;
  known_count?: number;
  unknown_count?: number;
  /** How many files Delete BIOS would remove: download records this plugin
   *  wrote whose file is still on disk. Deliberately not `local_count` — that is
   *  the library's progress ratio, which counts files nothing here put on disk
   *  and drops our own downloads once RomM stops listing them. */
  deletable_count?: number;
}

export interface FirmwareStatus {
  success: boolean;
  message?: string;
  server_offline?: boolean;
  platforms: FirmwarePlatformExt[];
}

export interface BiosFileStatus extends FirmwareVerdict {
  file_name: string;
  downloaded: boolean;
  local_path: string;
  /** Where the emulator said the file goes, relative to the firmware root —
   *  `dc/dc_boot.bin` where a subdirectory was declared, the bare name
   *  otherwise. `file_name` is its basename and `local_path` is it joined under
   *  a root this side does not know, so this is the only field that can answer
   *  which folder a file placed by hand belongs in. Optional because a payload
   *  from before the field existed carries none. */
  declared_path?: string;
  description: string;
  wanted: FirmwareWanted;
  /** Whether the core THIS game launches with requires the file. `wanted` is the
   *  machine's answer about the file; this one is the launch's. */
  required_by_active: boolean;
  cores?: Record<string, { required: boolean }>;
  used_by_active?: boolean;
  /** False for a file an emulator asks for that the RomM library does not hold.
   *  It still counts as missing — it just cannot be fetched from the plugin. */
  on_server?: boolean;
  /** The distribution whose own copy is sitting at the destination, as the
   *  resolver writes that distribution's name — printed verbatim, never mapped
   *  to a display form of ours. Absent claims nothing: the resolver states it
   *  only where it established the provenance. */
  supplied_by?: string | null;
}

/**
 * The backend's readiness decision for a platform's BIOS. `"unknown"` means no
 * readiness claim could be established, in one of three shapes: the server holds
 * firmware and not one file of it could be answered for; the platform holds no
 * file at all and its reading was not complete, which is already the case when a
 * single core the system offers went unread; or the launching core requires a
 * row nothing could judge (`required_withheld`) — a declared folder the resolver
 * could not read, say. A neutral state, never a green all-clear — and in the
 * third shape not a red one either, which is why the count comes with it.
 */
export type BiosLevel = "ok" | "partial" | "missing" | "unknown";

export interface BiosStatus {
  needs_bios: boolean;
  server_count?: number;
  local_count?: number;
  all_downloaded?: boolean;
  required_count?: number;
  required_downloaded?: number;
  /** How many of `required_count` nothing could judge — rows whose `satisfied`
   *  is null, such as a declared folder the resolver could not read. Nothing
   *  about the requirement was established, so such a row raises neither
   *  `required_downloaded` nor the count of files known to be absent — subtract
   *  it from `required_count` for that. Above zero, the readiness verdict
   *  declines (`bios_level` `"unknown"`) while the file rows keep their own
   *  answers. A row answered `false` is NOT here: that is a requirement shown to
   *  be unmet, and it reads red like any other. */
  required_withheld?: number;
  // Server files an installed emulator asks for, and files nothing could answer
  // about. A `not_needed` file is in neither — it is answered for, and wanted by
  // nothing — which is what keeps "nothing here is needed" apart from "nothing
  // could be established".
  known_count?: number;
  unknown_count?: number;
  files?: BiosFileStatus[];
  // unknown/ok/partial/missing state computed by the backend (compute_bios_level)
  // so the frontend reads the decision off the payload instead of re-deriving the
  // threshold logic. Present only when needs_bios is true.
  bios_level?: BiosLevel | null;
  // The compact token for that level (compute_bios_label), derived beside it so
  // the two can never disagree. Present only when needs_bios is true.
  bios_label?: string;
  // Set when the check could not determine the requirement — it found no file
  // to speak for AND its reading of the platform's emulators was not complete,
  // which one unread core is already enough for, so it cannot say that nothing
  // is wanted. A failed firmware fetch alone does not raise it: the listing is
  // caught and the file list simply comes back empty, which the machine's own
  // answer can still fill. The `needs_bios: false` it rides on is
  // ignorance, not an answer, so no consumer may clear a shown requirement on it
  // (#1693) — and the panel renders it as its own state rather than hiding the
  // BIOS tab, which would say the same thing (#1660).
  bios_status_unknown?: boolean;
}

export interface FirmwareDownloadResult {
  success: boolean;
  message?: string;
  file_path?: string;
  md5_match?: boolean | null;
  downloaded?: number;
  blocked_by_migration?: boolean;
}
