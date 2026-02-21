declare var SteamClient: {
  Apps: {
    AddShortcut(
      appName: string,
      exePath: string,
      startDir: string,
      launchArgs: string,
    ): Promise<number>;
    RemoveShortcut(appId: number): void;
    SetShortcutName(appId: number, name: string): void;
    SetShortcutExe(appId: number, exePath: string): void;
    SetShortcutStartDir(appId: number, startDir: string): void;
    SetAppLaunchOptions(appId: number, options: string): void;
    SetCustomArtworkForApp(
      appId: number,
      base64Data: string,
      imageType: "jpg" | "png",
      assetType: number,
    ): Promise<void>;
    ClearCustomArtworkForApp(appId: number, assetType: number): Promise<void>;
    RegisterForAppDetails(
      appId: number,
      callback: (details: any) => void,
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
    RegisterForOnSuspendRequest(callback: () => void): { unregister: () => void };
    RegisterForOnResumeFromSuspend(callback: () => void): { unregister: () => void };
  };
};

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
  deckDesktopApps: { apps: Map<number, any> };
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

declare var appDetailsStore: {
  GetDescriptions(appId: number): any;
  GetAssociations(appId: number): any;
  GetAppData(appId: number): any;
  SaveCustomLogoPosition(overview: any, position: any): void;
};

declare var appDetailsCache: {
  SetCachedDataForApp(appId: number, key: string, num: number, data: any): void;
};
