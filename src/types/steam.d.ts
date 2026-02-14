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
  };
};

interface AppStoreOverview {
  appid: number;
  display_name: string;
  strDisplayName: string;
}

interface SteamCollection {
  AsDragDropCollection(): {
    AddApps(overviews: AppStoreOverview[]): void;
    RemoveApps(overviews: AppStoreOverview[]): void;
  };
  Save(): Promise<void>;
  Delete(): Promise<void>;
  allApps: AppStoreOverview[];
  apps: { keys(): IterableIterator<number>; has(appId: number): boolean };
  displayName: string;
  id: string;
}

declare var collectionStore: {
  deckDesktopApps: { apps: Map<number, any> };
  userCollections: SteamCollection[];
  GetCollection(id: string): SteamCollection | undefined;
  GetCollectionIDByUserTag(tag: string): string | null;
  GetUserCollectionsByName(name: string): SteamCollection[];
  NewUnsavedCollection(tag: string, filter?: unknown, overviews?: AppStoreOverview[]): SteamCollection;
};

declare var appStore: {
  GetAppOverviewByAppID(appId: number): AppStoreOverview | null;
};
