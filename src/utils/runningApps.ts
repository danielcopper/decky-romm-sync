/**
 * Defensive running-app detection.
 *
 * Steam surfaces a game's running state through several page globals, and which
 * one is populated depends on the SteamOS build and on timing. After a
 * `plugin_loader` restart `Router.MainRunningApp` stays `null` for the whole
 * adoption window even though a game is running (#1054 / #1148 round 2 device
 * evidence) — it only (re)populates on a fresh lifecycle event, and our reloaded
 * JS context missed the one that started the game. So no single source is
 * trusted: every known surface is read through a guard, the readings merge into
 * one de-duped list, and a round that finds nothing still reports what EVERY
 * candidate returned (`diagnostics`) so the on-device log names the surface that
 * actually works on a given build.
 *
 * Every read is guarded. The globals are undeclared Steam SP values — a bare
 * reference to a truly-absent one throws `ReferenceError`, so presence is
 * probed with `typeof`; a present one may be `null`, a throwing getter, a plain
 * array, or a MobX observable (array-like/iterable). Any of those degrades to
 * "this source reported nothing", never a throw out of the reader.
 *
 * `Router` and `SteamUIStore` are the ambient Steam SP globals declared in
 * `types/steam.d.ts`.
 */

export interface RunningApp {
  appid: number;
  display_name: string;
}

export interface RunningAppsReading {
  /** Running apps merged + de-duped (by `appid`) across every list-shaped source. */
  apps: RunningApp[];
  /** Per-source diagnostic — what each candidate reported this round. */
  diagnostics: string;
}

interface SourceReading {
  label: string;
  note: string;
  apps: RunningApp[];
}

/** Coerce one candidate into a {@link RunningApp}, or `null` if it isn't one. */
function coerceRunningApp(value: unknown): RunningApp | null {
  if (typeof value !== "object" || value === null) return null;
  const rec = value as Record<string, unknown>;
  const appid = rec.appid;
  if (typeof appid !== "number") return null;
  const name =
    typeof rec.display_name === "string"
      ? rec.display_name
      : typeof rec.strDisplayName === "string"
        ? rec.strDisplayName
        : "";
  return { appid, display_name: name };
}

/**
 * Coerce a list-shaped source (plain array or MobX observable) into running
 * apps, dropping any entry that isn't a running app. Never throws — a
 * non-iterable or a throwing iterator yields an empty list.
 */
function coerceRunningAppList(value: unknown): RunningApp[] {
  if (value === null || value === undefined) return [];
  let items: unknown[];
  if (Array.isArray(value)) {
    items = value;
  } else if (typeof (value as { [Symbol.iterator]?: unknown })[Symbol.iterator] === "function") {
    items = Array.from(value as Iterable<unknown>);
  } else {
    return [];
  }
  const apps: RunningApp[] = [];
  for (const item of items) {
    const app = coerceRunningApp(item);
    if (app) apps.push(app);
  }
  return apps;
}

/** `Router.MainRunningApp` — the single foreground app (unreliable post-restart). */
function readRouterMainRunningApp(): SourceReading {
  const label = "Router.MainRunningApp";
  // NOSONAR(typescript:S7741) — Router is an undeclared Steam SP global; a direct
  // `=== undefined` would throw ReferenceError when it is genuinely absent.
  if (typeof Router === "undefined" || Router === null) return { label, note: "no-Router", apps: [] };
  try {
    const app = coerceRunningApp(Router.MainRunningApp);
    return { label, note: app ? `appid=${app.appid}` : "null", apps: app ? [app] : [] };
  } catch (e) {
    return { label, note: `threw:${e}`, apps: [] };
  }
}

/** `Router.RunningApps` — a running-apps array on some Steam builds. */
function readRouterRunningApps(): SourceReading {
  const label = "Router.RunningApps";
  // NOSONAR(typescript:S7741) — see readRouterMainRunningApp.
  if (typeof Router === "undefined" || Router === null) return { label, note: "no-Router", apps: [] };
  try {
    const apps = coerceRunningAppList(Router.RunningApps);
    return { label, note: describeList(apps, Router.RunningApps), apps };
  } catch (e) {
    return { label, note: `threw:${e}`, apps: [] };
  }
}

/** `SteamUIStore.RunningApps` — Steam's own UI store of running games. */
function readSteamUIStoreRunningApps(): SourceReading {
  const label = "SteamUIStore.RunningApps";
  // NOSONAR(typescript:S7741) — SteamUIStore is an undeclared Steam SP global.
  if (typeof SteamUIStore === "undefined" || SteamUIStore === null) return { label, note: "no-store", apps: [] };
  try {
    const apps = coerceRunningAppList(SteamUIStore.RunningApps);
    return { label, note: describeList(apps, SteamUIStore.RunningApps), apps };
  } catch (e) {
    return { label, note: `threw:${e}`, apps: [] };
  }
}

/** Diagnostic note for a list source — the appids found, or why nothing was. */
function describeList(apps: RunningApp[], raw: unknown): string {
  if (apps.length > 0) return `[${apps.map((a) => a.appid).join(",")}]`;
  if (raw === null || raw === undefined) return "absent";
  return "empty";
}

/**
 * Read every running-app source once, merging into a de-duped list plus a
 * per-source diagnostic string. `Router.MainRunningApp` is read first so, when
 * present, it heads the list (the foreground app), but its absence no longer
 * blinds detection — the list sources fill in after a loader restart.
 */
export function readRunningApps(): RunningAppsReading {
  const sources = [readRouterMainRunningApp(), readRouterRunningApps(), readSteamUIStoreRunningApps()];
  const merged = new Map<number, RunningApp>();
  for (const source of sources) {
    for (const app of source.apps) {
      if (!merged.has(app.appid)) merged.set(app.appid, app);
    }
  }
  return {
    apps: Array.from(merged.values()),
    diagnostics: sources.map((s) => `${s.label}=${s.note}`).join(" | "),
  };
}

/**
 * The primary running app (first source to report one) plus the round's
 * diagnostics. `null` app means no source saw a running app this round.
 */
export function readPrimaryRunningApp(): { app: RunningApp | null; diagnostics: string } {
  const reading = readRunningApps();
  return { app: reading.apps[0] ?? null, diagnostics: reading.diagnostics };
}

/** Is a specific `appId` currently running per ANY source? Never throws. */
export function isAppRunning(appId: number): boolean {
  return readRunningApps().apps.some((app) => app.appid === appId);
}

/** Is ANY app currently running per ANY source? Never throws. */
export function isAnyAppRunning(): boolean {
  return readRunningApps().apps.length > 0;
}
