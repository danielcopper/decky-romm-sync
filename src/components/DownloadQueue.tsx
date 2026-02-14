import { useState, useEffect, useRef, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ProgressBarWithInfo,
} from "@decky/ui";
import { getDownloadQueue, cancelDownload } from "../api/backend";
import { getDownloadState, setDownloads } from "../utils/downloadStore";
import type { DownloadItem } from "../types";

interface DownloadQueueProps {
  onBack: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export const DownloadQueue: FC<DownloadQueueProps> = ({ onBack }) => {
  const [downloads, setLocalDownloads] = useState<DownloadItem[]>([]);
  const [cleared, setCleared] = useState<Set<number>>(new Set());
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = () => {
    stopPolling();
    pollRef.current = setInterval(() => {
      setLocalDownloads([...getDownloadState()]);
    }, 500);
  };

  useEffect(() => {
    // Seed from backend on mount, then poll the store
    getDownloadQueue()
      .then((result) => {
        setDownloads(result.downloads);
        setLocalDownloads([...result.downloads]);
      })
      .catch(() => {
        // Fall back to whatever is in the store already
        setLocalDownloads([...getDownloadState()]);
      });
    startPolling();
    return () => stopPolling();
  }, []);

  const handleCancel = async (romId: number) => {
    try {
      await cancelDownload(romId);
    } catch {
      // ignore
    }
  };

  const handleClearCompleted = () => {
    const finishedIds = downloads
      .filter((d) => d.status === "completed" || d.status === "failed" || d.status === "cancelled")
      .map((d) => d.rom_id);
    setCleared((prev) => {
      const next = new Set(prev);
      finishedIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const visible = downloads.filter((d) => !cleared.has(d.rom_id));
  const active = visible.filter(
    (d) => d.status === "queued" || d.status === "downloading"
  );
  const finished = visible.filter(
    (d) => d.status === "completed" || d.status === "failed" || d.status === "cancelled"
  );
  const hasFinished = downloads.some(
    (d) =>
      !cleared.has(d.rom_id) &&
      (d.status === "completed" || d.status === "failed" || d.status === "cancelled")
  );

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Downloads">
        {visible.length === 0 ? (
          <PanelSectionRow>
            <Field label="No downloads" />
          </PanelSectionRow>
        ) : (
          <>
            {active.map((item) => (
              <PanelSectionRow key={item.rom_id}>
                <ProgressBarWithInfo
                  nProgress={
                    item.total_bytes > 0
                      ? item.bytes_downloaded / item.total_bytes
                      : undefined
                  }
                  indeterminate={item.total_bytes === 0}
                  sOperationText={`${item.rom_name} (${item.platform_name})`}
                  sTimeRemaining={
                    item.total_bytes > 0
                      ? `${formatBytes(item.bytes_downloaded)} / ${formatBytes(item.total_bytes)}`
                      : formatBytes(item.bytes_downloaded)
                  }
                />
              </PanelSectionRow>
            ))}
            {active.map((item) => (
              <PanelSectionRow key={`cancel-${item.rom_id}`}>
                <ButtonItem
                  layout="below"
                  onClick={() => handleCancel(item.rom_id)}
                >
                  Cancel {item.rom_name}
                </ButtonItem>
              </PanelSectionRow>
            ))}

            {finished.map((item) => (
              <PanelSectionRow key={item.rom_id}>
                <Field
                  label={item.rom_name}
                  description={
                    item.status === "completed"
                      ? `Completed — ${formatBytes(item.total_bytes)}`
                      : item.status === "failed"
                        ? `Failed${item.error ? `: ${item.error}` : ""}`
                        : "Cancelled"
                  }
                />
              </PanelSectionRow>
            ))}

            {hasFinished && (
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={handleClearCompleted}>
                  Clear Completed
                </ButtonItem>
              </PanelSectionRow>
            )}
          </>
        )}
      </PanelSection>
    </>
  );
};
