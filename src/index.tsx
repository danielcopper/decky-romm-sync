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
import { initSyncManager } from "./utils/syncManager";
import { setSyncProgress } from "./utils/syncProgress";
import type { SyncProgress } from "./types";

type Page = "main" | "connection" | "platforms" | "danger";

const QAMPanel: FC = () => {
  const [page, setPage] = useState<Page>("main");

  switch (page) {
    case "connection":
      return <ConnectionSettings onBack={() => setPage("main")} />;
    case "platforms":
      return <PlatformSync onBack={() => setPage("main")} />;
    case "danger":
      return <DangerZone onBack={() => setPage("main")} />;
    default:
      return <MainPage onNavigate={(p) => setPage(p)} />;
  }
};

export default definePlugin(() => {
  const onSyncComplete = (data: {
    platform_app_ids: Record<string, number[]>;
    total_games: number;
  }) => {
    console.log("[RomM] sync_complete received:", data.total_games, "games");
    toaster.toast({
      title: "RomM Library",
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

  return {
    name: "RomM Library",
    icon: <FaGamepad />,
    content: <QAMPanel />,
    onDismount() {
      removeEventListener("sync_complete", syncCompleteListener);
      removeEventListener("sync_apply", syncApplyListener);
      removeEventListener("sync_progress", syncProgressListener);
    },
  };
});
