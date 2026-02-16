import {
  definePlugin,
  addEventListener,
  removeEventListener,
  toaster,
} from "@decky/api";
import { useState, FC } from "react";
import { FaGamepad } from "react-icons/fa";
import { MainPage } from "./components/MainPage";
import { ConnectionSettings } from "./components/ConnectionSettings";
import { PlatformSync } from "./components/PlatformSync";
import { DangerZone } from "./components/DangerZone";
import { DownloadQueue } from "./components/DownloadQueue";
import { BiosManager } from "./components/BiosManager";
import { initSyncManager } from "./utils/syncManager";
import { setSyncProgress } from "./utils/syncProgress";
import { updateDownload } from "./utils/downloadStore";
import { registerGameDetailPatch, unregisterGameDetailPatch } from "./patches/gameDetailPatch";
import type { SyncProgress, DownloadProgressEvent, DownloadCompleteEvent } from "./types";

type Page = "main" | "connection" | "platforms" | "danger" | "downloads" | "bios";

// Module-level page state survives QAM remounts (e.g. after modal close)
let currentPage: Page = "main";

const QAMPanel: FC = () => {
  const [page, setPageState] = useState<Page>(currentPage);
  const setPage = (p: Page) => { currentPage = p; setPageState(p); };

  switch (page) {
    case "connection":
      return <ConnectionSettings onBack={() => setPage("main")} />;
    case "platforms":
      return <PlatformSync onBack={() => setPage("main")} />;
    case "danger":
      return <DangerZone onBack={() => setPage("main")} />;
    case "downloads":
      return <DownloadQueue onBack={() => setPage("main")} />;
    case "bios":
      return <BiosManager onBack={() => setPage("main")} />;
    default:
      return <MainPage onNavigate={(p) => setPage(p)} />;
  }
};

export default definePlugin(() => {
  registerGameDetailPatch();

  const onSyncComplete = (data: {
    platform_app_ids: Record<string, number[]>;
    total_games: number;
  }) => {
    console.log("[RomM] sync_complete received:", data.total_games, "games");
    toaster.toast({
      title: "RomM Sync",
      body: `Sync complete! ${data.total_games} games added.`,
    });
  };

  const syncCompleteListener = addEventListener<
    [{ platform_app_ids: Record<string, number[]>; total_games: number }]
  >("sync_complete", onSyncComplete);

  const syncApplyListener = initSyncManager();

  // Backend emits sync_progress events throughout _do_sync — update the module-level store
  const syncProgressListener = addEventListener<[SyncProgress]>(
    "sync_progress",
    (progress: SyncProgress) => {
      setSyncProgress(progress);
    }
  );

  const downloadProgressListener = addEventListener<[DownloadProgressEvent]>(
    "download_progress",
    (data: DownloadProgressEvent) => {
      updateDownload({
        rom_id: data.rom_id,
        rom_name: data.rom_name,
        platform_name: "",
        file_name: "",
        status: data.status as "queued" | "downloading" | "completed" | "failed" | "cancelled",
        progress: data.progress,
        bytes_downloaded: data.bytes_downloaded,
        total_bytes: data.total_bytes,
      });
    }
  );

  const downloadCompleteListener = addEventListener<[DownloadCompleteEvent]>(
    "download_complete",
    (data: DownloadCompleteEvent) => {
      updateDownload({
        rom_id: data.rom_id,
        rom_name: data.rom_name,
        platform_name: data.platform_name,
        file_name: "",
        status: "completed",
        progress: 1,
        bytes_downloaded: 0,
        total_bytes: 0,
      });
      toaster.toast({
        title: "RomM Sync",
        body: `Downloaded ${data.rom_name}`,
      });
    }
  );

  return {
    name: "RomM Sync",
    icon: <FaGamepad />,
    content: <QAMPanel />,
    alwaysRender: true,
    onDismount() {
      unregisterGameDetailPatch();
      removeEventListener("sync_complete", syncCompleteListener);
      removeEventListener("sync_apply", syncApplyListener);
      removeEventListener("sync_progress", syncProgressListener);
      removeEventListener("download_progress", downloadProgressListener);
      removeEventListener("download_complete", downloadCompleteListener);
    },
  };
});
