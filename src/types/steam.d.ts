declare var SteamClient: {
  Apps: {
    AddShortcut(appName: string, exePath: string, startDir: string, launchArgs: string): Promise<number>;
    RemoveShortcut(appId: number): void;
    SetShortcutName(appId: number, name: string): void;
    SetShortcutExe(appId: number, exePath: string): void;
    SetShortcutStartDir(appId: number, startDir: string): void;
    SetShortcutIcon(appId: number, path: string): void;
    SetAppLaunchOptions(appId: number, options: string): void;
    OpenAppSettingsDialog(appId: number, section: string): void;
    SetCustomArtworkForApp(
      appId: number,
      base64Data: string,
      imageType: "jpg" | "png",
      assetType: number,
    ): Promise<void>;
    ClearCustomArtworkForApp(appId: number, assetType: number): Promise<void>;
    // The runtime may invoke the callback with no details before the app's
    // data is loaded — `details` is genuinely absent on early fires.
    RegisterForAppDetails(
      appId: number,
      callback: (details: SteamAppDetails | undefined) => void,
    ): { unregister: () => void };
    RunGame(gameId: string | number, launchId: string, param2: number, param3: number): void;
    TerminateApp(appId: number, force: boolean): void;
    RegisterForGameActionStart(
      callback: (gameActionId: number, appIdStr: string, action: string, launchSource: number) => void,
    ): { unregister: () => void };
    CancelGameAction(gameActionId: number): void;
  };
  GameSessions: {
    RegisterForAppLifetimeNotifications(
      callback: (update: { unAppID: number; nInstanceID: number; bRunning: boolean }) => void,
    ): { unregister: () => void };
  };
  System: {
    GetSystemInfo(): Promise<{ sHostname: string; [key: string]: any }>;
  };
  User: {
    // Restart the whole Steam client (closes and reopens Steam). `force` skips the
    // "are you sure" path. Used as the deterministic "free memory" action — a full
    // client restart resets the renderer's per-session heap budget (#1383).
    StartRestart(force: boolean): void;
  };
};

interface SteamAppDetails {
  // The two launch-options fields the runtime exposes — keys vary by Steam
  // build, so we accept either. Anything else is intentionally untyped here:
  // consumers should narrow before reading.
  strLaunchOptions?: string;
  LaunchOptions?: string;
  // The shortcut's executable path. Used as the RomM ownership marker:
  // shortcuts whose exe ends in `/bin/rom-launcher` are ours.
  strShortcutExe?: string;
}

interface SteamPerClientData {
  clientid: string;
  client_name: string;
  installed: boolean;
  streaming_to_local_client?: boolean;
}

interface SteamAppOverview {
  appid: number;
  display_name: string;
  strDisplayName: string;
  app_type?: number;
  controller_support?: number;
  metacritic_score?: number;
  minutes_playtime_forever?: number;
  minutes_playtime_last_two_weeks?: number;
  rt_last_time_played?: number;
  rt_last_time_played_or_installed?: number;
  // Epoch-seconds cache-buster for the library tile's custom-image URL
  // (`/customimage/{appid}?v={rt_custom_image_mtime}`). A full client restart
  // normally stamps it; the cover nudge stamps it per created shortcut so a
  // freshly-written grid cover is picked up on the tile's next render.
  rt_custom_image_mtime?: number;
  m_setStoreCategories?: Set<number>;
  local_per_client_data?: SteamPerClientData;
  per_client_data?: SteamPerClientData[];
  GetCanonicalReleaseDate?(): number;
  BHasStoreCategory?(category: number): boolean;
  BIsModOrShortcut?(): boolean;
  BHasRecentlyLaunched?(): boolean;
  GetGameID?(): string;
  GetPrimaryAppID?(): number;
}

// Keep the old name as an alias for backwards compatibility with existing code
type AppStoreOverview = SteamAppOverview;

interface SteamCollection {
  AsDragDropCollection(): {
    AddApps(overviews: SteamAppOverview[]): void;
    RemoveApps(overviews: SteamAppOverview[]): void;
  };
  Save(): Promise<void>;
  Delete(): Promise<void>;
  allApps: SteamAppOverview[];
  apps: { keys(): IterableIterator<number>; has(appId: number): boolean };
  displayName: string;
  id: string;
}

declare var collectionStore: {
  // Populated asynchronously by Steam — absent until the desktop-apps
  // collection is built, so reads must guard.
  deckDesktopApps?: { apps: Map<number, any> };
  localGamesCollection?: { apps: Map<number, any> };
  userCollections: SteamCollection[];
  GetCollection(id: string): SteamCollection | undefined;
  GetCollectionIDByUserTag(tag: string): string | null;
  GetUserCollectionsByName(name: string): SteamCollection[];
  NewUnsavedCollection(tag: string, filter?: unknown, overviews?: SteamAppOverview[]): SteamCollection;
};

declare var appStore: {
  GetAppOverviewByAppID(appId: number): SteamAppOverview | null;
  allApps: SteamAppOverview[];
};

// Running-app surfaces read by the defensive `utils/runningApps` reader. Steam SP
// globals — genuinely absent (hence `undefined`) or `null` on some builds/timing,
// so every read guards. `RunningApps` is optional: present only on builds that
// expose it (Router.MainRunningApp stays authoritative for the foreground app).
declare var Router:
  | {
      MainRunningApp: SteamAppOverview | null;
      RunningApps?: SteamAppOverview[];
    }
  | null
  | undefined;

declare var SteamUIStore:
  | {
      RunningApps?: SteamAppOverview[];
      // Focus a running app in gamescope — pure UI selection, not a launch. The
      // state-aware Resume button (#1313) calls this + NavigateToRunningApp to
      // foreground a live session (Steam's own "Resume Game" path).
      SetRunningApp(appId: number): void;
      // Navigate to the running-app screen. Optional — absent on older SteamUI
      // builds, where the Resume path falls back to Navigation.Navigate("/apprunning").
      NavigateToRunningApp?(force?: boolean): void;
    }
  | null
  | undefined;

declare var appDetailsStore: {
  GetDescriptions(appId: number): any;
  GetAssociations(appId: number): any;
  GetAppData(appId: number): any;
  SaveCustomLogoPosition(overview: any, position: any): void;
};

declare var appDetailsCache: {
  SetCachedDataForApp(appId: number, key: string, num: number, data: any): void;
};

interface MobxGlobals {
  /** Mobx safety gate — flipped true around state mutations on Steam's stores. */
  allowStateChanges: boolean;
}

/**
 * Steam injects mobx onto the page; `__mobxGlobals` is the singleton state
 * holder. Declared on `globalThis` so callers can read it without an
 * unchecked cast.
 */
declare var __mobxGlobals: MobxGlobals | undefined;
