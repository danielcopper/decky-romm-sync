import { useState, useEffect, useRef, FC } from "react";
import { addEventListener, removeEventListener, toaster } from "@decky/api";
import { Focusable, DialogButton, showModal, ModalRoot } from "@decky/ui";
import {
  getRomBySteamAppId,
  getInstalledRom,
  startDownload,
  cancelDownload,
  removeRom,
  checkPlatformBios,
  getSgdbArtworkBase64,
  getRomMetadata,
} from "../api/backend";
import { updateMetadataForApp } from "../patches/metadataPatches";
import type { InstalledRom, DownloadProgressEvent, DownloadCompleteEvent, BiosStatus } from "../types";

interface GameDetailPanelProps {
  appId: number;
}

interface RomInfo {
  rom_id: number;
  name: string;
  platform_name: string;
  platform_slug: string;
}

type PanelState = "loading" | "not_romm" | "not_installed" | "downloading" | "installed";

const styles = {
  container: {
    padding: "12px 16px",
    margin: "8px 0",
    background: "linear-gradient(135deg, rgba(62, 39, 120, 0.4), rgba(30, 60, 114, 0.4))",
    borderRadius: "4px",
    border: "1px solid rgba(255, 255, 255, 0.08)",
  } as const,
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: "8px",
  } as const,
  title: {
    fontSize: "12px",
    fontWeight: "bold" as const,
    color: "rgba(255, 255, 255, 0.6)",
    textTransform: "uppercase" as const,
    letterSpacing: "0.5px",
  },
  statusBadge: (color: string) => ({
    fontSize: "11px",
    fontWeight: "bold" as const,
    padding: "2px 8px",
    borderRadius: "3px",
    background: color,
    color: "#fff",
  }),
  info: {
    fontSize: "13px",
    color: "rgba(255, 255, 255, 0.8)",
    marginBottom: "4px",
  } as const,
  subtext: {
    fontSize: "11px",
    color: "rgba(255, 255, 255, 0.4)",
    marginBottom: "8px",
  } as const,
  button: {
    padding: "6px 16px",
    fontSize: "13px",
    fontWeight: "bold" as const,
    minWidth: "auto",
    width: "auto",
  } as const,
  progressContainer: {
    marginBottom: "8px",
  } as const,
  progressBar: {
    width: "100%",
    height: "6px",
    background: "rgba(255, 255, 255, 0.1)",
    borderRadius: "3px",
    overflow: "hidden" as const,
    marginBottom: "4px",
  } as const,
  progressFill: (pct: number) => ({
    width: `${pct}%`,
    height: "100%",
    background: "linear-gradient(90deg, #1a9fff, #4fc3f7)",
    borderRadius: "3px",
    transition: "width 0.3s ease",
  }),
  progressText: {
    fontSize: "11px",
    color: "rgba(255, 255, 255, 0.5)",
  } as const,
};

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

export const GameDetailPanel: FC<GameDetailPanelProps> = ({ appId }) => {
  const [state, setState] = useState<PanelState>("loading");
  const [romInfo, setRomInfo] = useState<RomInfo | null>(null);
  const [installed, setInstalled] = useState<InstalledRom | null>(null);
  const [progress, setProgress] = useState(0);
  const [bytesDownloaded, setBytesDownloaded] = useState(0);
  const [totalBytes, setTotalBytes] = useState(0);
  const [actionPending, setActionPending] = useState(false);
  const [biosStatus, setBiosStatus] = useState<BiosStatus | null>(null);
  const [artworkLoading, setArtworkLoading] = useState(false);
  const romIdRef = useRef<number | null>(null);

  const fetchSgdbArtwork = async (romId: number, steamAppId: number, showToast = false) => {
    setArtworkLoading(true);
    const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
    for (const assetType of [1, 2, 3] as const) {
      try {
        const result = await getSgdbArtworkBase64(romId, assetType);
        if (result.no_api_key) {
          if (showToast) {
            toaster.toast({
              title: "RomM Sync",
              body: "Extra artwork requires a SteamGridDB API key. Configure it in plugin settings.",
            });
          }
          setArtworkLoading(false);
          return;
        }
        if (result.base64) {
          await SteamClient.Apps.SetCustomArtworkForApp(steamAppId, result.base64, "png", assetType);
          console.log(`[RomM] Set SGDB artwork type ${assetType} for appId=${steamAppId}`);

          // Save default logo position after setting logo
          if (assetType === 2) {
            try {
              const overview = appStore.GetAppOverviewByAppID(steamAppId);
              if (overview && appDetailsStore?.SaveCustomLogoPosition) {
                appDetailsStore.SaveCustomLogoPosition(overview, {
                  pinnedPosition: "BottomLeft", nWidthPct: 50, nHeightPct: 50,
                });
              }
            } catch { /* appStore/appDetailsStore may not be available */ }
          }
        }
        await delay(50);
      } catch (err) {
        console.error(`[RomM] Failed to fetch/set SGDB artwork type ${assetType}:`, err);
      }
    }
    setArtworkLoading(false);
  };

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const rom = await getRomBySteamAppId(appId);
        if (cancelled) return;

        if (!rom) {
          setState("not_romm");
          return;
        }

        setRomInfo({
          rom_id: rom.rom_id,
          name: rom.name,
          platform_name: rom.platform_name,
          platform_slug: rom.platform_slug || "",
        });
        romIdRef.current = rom.rom_id;

        // Check BIOS status for this platform
        if (rom.platform_slug) {
          try {
            const bios = await checkPlatformBios(rom.platform_slug);
            if (!cancelled) setBiosStatus(bios);
          } catch {
            // non-critical, ignore
          }
        }

        const inst = await getInstalledRom(rom.rom_id);
        if (cancelled) return;

        if (inst) {
          setInstalled(inst);
          setState("installed");
        } else {
          setState("not_installed");
        }

        // Fetch SGDB artwork on-demand (hero, logo, wide grid)
        fetchSgdbArtwork(rom.rom_id, appId);

        // Fetch and apply metadata for native Steam display
        try {
          const metadata = await getRomMetadata(rom.rom_id);
          if (!cancelled && metadata) {
            updateMetadataForApp(appId, rom.rom_id, metadata);
          }
        } catch {
          // non-critical, metadata patches still work from cache
        }
      } catch (e) {
        console.error("[RomM] GameDetailPanel load error:", e);
        if (!cancelled) setState("not_romm");
      }
    }

    load();
    return () => { cancelled = true; };
  }, [appId]);

  // Listen for download progress events
  useEffect(() => {
    const progressListener = addEventListener<[DownloadProgressEvent]>(
      "download_progress",
      (evt: DownloadProgressEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        if (evt.status === "downloading") {
          setState("downloading");
          setProgress(evt.progress);
          setBytesDownloaded(evt.bytes_downloaded);
          setTotalBytes(evt.total_bytes);
        } else if (evt.status === "failed" || evt.status === "cancelled") {
          setState("not_installed");
          setActionPending(false);
        }
      },
    );

    const completeListener = addEventListener<[DownloadCompleteEvent]>(
      "download_complete",
      (evt: DownloadCompleteEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        setState("installed");
        setActionPending(false);
        setInstalled({
          rom_id: evt.rom_id,
          file_name: evt.file_path.split("/").pop() || "",
          file_path: evt.file_path,
          system: "",
          platform_slug: "",
          installed_at: new Date().toISOString(),
        });
      },
    );

    return () => {
      removeEventListener("download_progress", progressListener);
      removeEventListener("download_complete", completeListener);
    };
  }, []);

  const handleDownload = async () => {
    if (!romInfo || actionPending) return;
    setActionPending(true);
    try {
      const result = await startDownload(romInfo.rom_id);
      if (result.success) {
        setState("downloading");
        setProgress(0);
      } else {
        setActionPending(false);
      }
    } catch {
      setActionPending(false);
    }
  };

  const handleCancel = async () => {
    if (!romInfo) return;
    try {
      await cancelDownload(romInfo.rom_id);
    } catch {
      // ignore
    }
  };

  const handleUninstall = async () => {
    if (!romInfo || actionPending) return;
    setActionPending(true);
    try {
      const result = await removeRom(romInfo.rom_id);
      if (result.success) {
        setState("not_installed");
        setInstalled(null);
      }
    } catch {
      // ignore
    }
    setActionPending(false);
  };

  // Not a RomM game or still loading
  if (state === "loading" || state === "not_romm") return null;

  const statusColor =
    state === "installed"
      ? "rgba(76, 175, 80, 0.8)"
      : state === "downloading"
        ? "rgba(33, 150, 243, 0.8)"
        : "rgba(158, 158, 158, 0.6)";

  const statusLabel =
    state === "installed"
      ? "Installed"
      : state === "downloading"
        ? "Downloading"
        : "Not Installed";

  return (
    <Focusable style={styles.container}>
      <div style={styles.header}>
        <span style={styles.title}>RomM Sync</span>
        <span style={styles.statusBadge(statusColor)}>{statusLabel}</span>
      </div>

      {romInfo && (
        <div style={styles.info}>
          {romInfo.platform_name}
        </div>
      )}

      {biosStatus?.needs_bios && (
        <Focusable
          style={{
            fontSize: "12px",
            color: biosStatus.all_downloaded ? "#81c784" : "#ffb74d",
            padding: "4px 8px",
            marginBottom: "8px",
            background: biosStatus.all_downloaded
              ? "rgba(76, 175, 80, 0.15)"
              : "rgba(255, 152, 0, 0.15)",
            borderRadius: "3px",
            border: `1px solid ${biosStatus.all_downloaded
              ? "rgba(76, 175, 80, 0.3)"
              : "rgba(255, 152, 0, 0.3)"}`,
            cursor: "pointer",
          }}
          onActivate={() => {
            const files = biosStatus.files || [];
            showModal(
              <ModalRoot>
                <div style={{ padding: "16px" }}>
                  <div style={{
                    fontSize: "16px",
                    fontWeight: "bold",
                    marginBottom: "12px",
                    color: "#fff",
                  }}>
                    BIOS Files — {romInfo?.platform_name}
                  </div>
                  <div style={{
                    fontSize: "12px",
                    color: "rgba(255, 255, 255, 0.5)",
                    marginBottom: "12px",
                  }}>
                    {biosStatus.local_count}/{biosStatus.server_count} downloaded
                  </div>
                  {files.map((f) => (
                    <div
                      key={f.file_name}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        padding: "6px 0",
                        borderBottom: "1px solid rgba(255, 255, 255, 0.08)",
                      }}
                    >
                      <span style={{
                        fontSize: "13px",
                        color: "rgba(255, 255, 255, 0.9)",
                      }}>
                        {f.file_name}
                      </span>
                      <span style={{
                        fontSize: "12px",
                        color: f.downloaded ? "#81c784" : "#ffb74d",
                        fontWeight: "bold",
                      }}>
                        {f.downloaded ? "\u2713" : "Missing"}
                      </span>
                    </div>
                  ))}
                </div>
              </ModalRoot>
            );
          }}
        >
          {biosStatus.all_downloaded
            ? `BIOS ready (${biosStatus.server_count} file${biosStatus.server_count !== 1 ? "s" : ""}) \u203a`
            : `BIOS required — ${biosStatus.local_count}/${biosStatus.server_count} downloaded \u203a`}
        </Focusable>
      )}

      {state === "downloading" && (
        <div style={styles.progressContainer}>
          <div style={styles.progressBar}>
            <div style={styles.progressFill(progress)} />
          </div>
          <span style={styles.progressText}>
            {progress.toFixed(0)}%
            {totalBytes > 0 && ` — ${formatBytes(bytesDownloaded)} / ${formatBytes(totalBytes)}`}
          </span>
        </div>
      )}

      {installed && state === "installed" && (
        <div style={styles.subtext}>{installed.file_path}</div>
      )}

      <Focusable style={{ display: "flex", gap: "8px" }}>
        {state === "not_installed" && (
          <DialogButton
            style={styles.button}
            onClick={handleDownload}
            disabled={actionPending}
          >
            {actionPending ? "Starting..." : "Download"}
          </DialogButton>
        )}
        {state === "downloading" && (
          <DialogButton
            style={styles.button}
            onClick={handleCancel}
          >
            Cancel
          </DialogButton>
        )}
        {state === "installed" && (
          <DialogButton
            style={styles.button}
            onClick={handleUninstall}
            disabled={actionPending}
          >
            {actionPending ? "Removing..." : "Uninstall"}
          </DialogButton>
        )}
        {romInfo && (
          <DialogButton
            style={styles.button}
            onClick={async () => {
              fetchSgdbArtwork(romInfo.rom_id, appId, true);
              try {
                const metadata = await getRomMetadata(romInfo.rom_id);
                if (metadata) updateMetadataForApp(appId, romInfo.rom_id, metadata);
              } catch {
                // non-critical
              }
            }}
            disabled={artworkLoading}
          >
            {artworkLoading ? "Loading..." : "Refresh Metadata"}
          </DialogButton>
        )}
      </Focusable>
    </Focusable>
  );
};
