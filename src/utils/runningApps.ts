/**
 * Defensive running-app detection.
 *
 * `SteamUIStore.RunningApps` is the running-app surface — a genuine ambient
 * Steam SP global (declared in `types/steam.d.ts`), and the only one there is.
 * Do not add a second: `Router` is not a page global on any SteamUI build (this
 * reader probed it until #1588 and every logged round said `no-Router`), and
 * `@decky/ui`'s `Router` export resolves to this very `SteamUIStore` singleton.
 *
 * The list is a MEMBERSHIP set, not a launch order. The store maps its private
 * running-appid array through the app store and DROPS entries whose overview has
 * not loaded yet, so the head of `RunningApps` is not even reliably Steam's own
 * `MainRunningApp` — the two diverge exactly during the post-launch window this
 * plugin cares about. And the ordering it does carry is "most recently
 * FOREGROUNDED" (`SetRunningApp` removes and unshifts), while the reconciler that
 * notices a newly-launched process APPENDS it at the tail. The head is therefore
 * never a way to identify the app that just started — use the appid the lifetime
 * notification carries. Consumers here ask membership questions only.
 *
 * The read is still guarded. A bare reference to a truly-absent SP global
 * throws `ReferenceError`, so presence is probed with `typeof`; a present store
 * may be `null`, expose a throwing getter, or hand back a plain array or a MobX
 * observable (array-like/iterable). Any of those degrades to "nothing running",
 * never a throw out of the reader.
 *
 * The store can also legitimately report EMPTY while a game is running: after a
 * `plugin_loader` restart the reloaded JS context sees `RunningApps` empty for
 * several seconds with the game still up (#1054 / #1148 round 2 device
 * evidence). So a single empty round proves nothing — the adoption path polls,
 * and every round reports what the store said (`diagnostics`: absent / empty /
 * threw / the appids found) so the on-device log can tell those cases apart.
 */

export interface RunningApp {
  appid: number;
  display_name: string;
}

export interface RunningAppsReading {
  /** The running apps the store reported this round, in store order. */
  apps: RunningApp[];
  /** Diagnostic — what the store reported this round. */
  diagnostics: string;
}

const SOURCE_LABEL = "SteamUIStore.RunningApps";

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

/** Diagnostic note for the list — the appids found, or why none were. */
function describeList(apps: RunningApp[], raw: unknown): string {
  if (apps.length > 0) return `[${apps.map((a) => a.appid).join(",")}]`;
  if (raw === null || raw === undefined) return "absent";
  return "empty";
}

/**
 * Read `SteamUIStore.RunningApps` once, as a list plus a diagnostic naming what
 * the store reported. Never throws: an absent store, a `null` store, a throwing
 * getter and a non-list value all read as "nothing running" with a note saying
 * which.
 */
export function readRunningApps(): RunningAppsReading {
  // NOSONAR(typescript:S7741) — SteamUIStore is an undeclared Steam SP global; a
  // direct `=== undefined` would throw ReferenceError when it is genuinely absent.
  if (typeof SteamUIStore === "undefined" || SteamUIStore === null) {
    return { apps: [], diagnostics: `${SOURCE_LABEL}=no-store` };
  }
  try {
    // One getter read — re-reading for the diagnostic could observe a different
    // value, or throw outside the coercion it describes.
    const raw: unknown = SteamUIStore.RunningApps;
    const apps = coerceRunningAppList(raw);
    return { apps, diagnostics: `${SOURCE_LABEL}=${describeList(apps, raw)}` };
  } catch (e) {
    return { apps: [], diagnostics: `${SOURCE_LABEL}=threw:${e}` };
  }
}

/** Is a specific `appId` currently running per the store? Never throws. */
export function isAppRunning(appId: number): boolean {
  return readRunningApps().apps.some((app) => app.appid === appId);
}

/** Is ANY app currently running per the store? Never throws. */
export function isAnyAppRunning(): boolean {
  return readRunningApps().apps.length > 0;
}
