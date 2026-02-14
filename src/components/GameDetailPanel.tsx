import { useState, useEffect, useRef, FC } from "react";
import { addEventListener, removeEventListener } from "@decky/api";
import {
  getRomBySteamAppId,
  getInstalledRom,
  startDownload,
  cancelDownload,
  removeRom,
} from "../api/backend";
import type { InstalledRom, DownloadProgressEvent, DownloadCompleteEvent } from "../types";

interface GameDetailPanelProps {
  appId: number;
}

interface RomInfo {
  rom_id: number;
  name: string;
  platform_name: string;
  file_name: string;
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
  button: (bg: string) => ({
    display: "inline-block",
    padding: "6px 16px",
    fontSize: "13px",
    fontWeight: "bold" as const,
    color: "#fff",
    background: bg,
    border: "none",
    borderRadius: "3px",
    cursor: "pointer",
    marginRight: "8px",
  }),
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
  const romIdRef = useRef<number | null>(null);

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
          file_name: rom.file_name,
        });
        romIdRef.current = rom.rom_id;

        const inst = await getInstalledRom(rom.rom_id);
        if (cancelled) return;

        if (inst) {
          setInstalled(inst);
          setState("installed");
        } else {
          setState("not_installed");
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
    <div style={styles.container}>
      <div style={styles.header}>
        <span style={styles.title}>RomM Library</span>
        <span style={styles.statusBadge(statusColor)}>{statusLabel}</span>
      </div>

      {romInfo && (
        <div style={styles.info}>
          {romInfo.platform_name} &middot; {romInfo.file_name}
        </div>
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

      <div>
        {state === "not_installed" && (
          <button
            style={styles.button("rgba(33, 150, 243, 0.9)")}
            onClick={handleDownload}
            disabled={actionPending}
          >
            {actionPending ? "Starting..." : "Download"}
          </button>
        )}
        {state === "downloading" && (
          <button
            style={styles.button("rgba(244, 67, 54, 0.8)")}
            onClick={handleCancel}
          >
            Cancel
          </button>
        )}
        {state === "installed" && (
          <button
            style={styles.button("rgba(244, 67, 54, 0.7)")}
            onClick={handleUninstall}
            disabled={actionPending}
          >
            {actionPending ? "Removing..." : "Uninstall"}
          </button>
        )}
      </div>
    </div>
  );
};
