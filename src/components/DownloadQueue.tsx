import { useState, useEffect, useRef, FC, Fragment } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, Field, ProgressBar } from "@decky/ui";
import { getDownloadQueue, cancelDownload, pauseDownload, resumeDownload } from "../api/backend";
import { getDownloadState, setDownloads } from "../utils/downloadStore";
import { formatBytes } from "../utils/formatters";
import { scrollToTop } from "../utils/scrollHelpers";
import { detach } from "../utils/detach";
import type { DownloadItem } from "../types";

interface DownloadQueueProps {
  onBack: () => void;
}

function formatFinishedDescription(item: DownloadItem): string {
  if (item.status === "completed") return `Completed — ${formatBytes(item.total_bytes)}`;
  if (item.status === "failed") {
    const detail = item.error ? `: ${item.error}` : "";
    return `Failed${detail}`;
  }
  return "Cancelled";
}

export const DownloadQueue: FC<DownloadQueueProps> = ({ onBack }) => {
  const [downloads, setLocalDownloads] = useState<DownloadItem[]>([]); // NOSONAR(typescript:S6754) — setter intentionally renamed (local wrapper around global download state).
  const [cleared, setCleared] = useState<Set<number>>(new Set());
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  // Un-clear rom_ids that have new active downloads (re-download case)
  const unclearRestarted =
    (current: DownloadItem[]) =>
    (prev: Set<number>): Set<number> => {
      const restarted = current.filter(
        (d) => (d.status === "downloading" || d.status === "queued" || d.status === "paused") && prev.has(d.rom_id),
      );
      if (restarted.length === 0) return prev;
      const next = new Set(prev);
      for (const d of restarted) next.delete(d.rom_id);
      return next;
    };

  const pollTick = () => {
    const current = getDownloadState();
    setCleared(unclearRestarted(current));
    setLocalDownloads([...current]);
  };

  const startPolling = () => {
    stopPolling();
    pollRef.current = setInterval(pollTick, 500);
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

  const handlePause = async (romId: number) => {
    try {
      await pauseDownload(romId);
    } catch {
      // ignore
    }
  };

  const handleResume = async (romId: number) => {
    try {
      await resumeDownload(romId);
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
  // Paused downloads stay in the active section — they're not finished, the
  // partial transfer is held for resume.
  const active = visible.filter((d) => d.status === "queued" || d.status === "downloading" || d.status === "paused");
  const finished = visible.filter((d) => d.status === "completed" || d.status === "failed" || d.status === "cancelled");
  const hasFinished = downloads.some(
    (d) => !cleared.has(d.rom_id) && (d.status === "completed" || d.status === "failed" || d.status === "cancelled"),
  );

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={onBack}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
          >
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
                {/* Own the caption in a full-width row and use the bare ProgressBar.
                    ProgressBarWithInfo is a Steam Field (label column | bar column);
                    with the rom name in sOperationText the empty bar column gets
                    squeezed into the right half and clips (#751). The bare
                    ProgressBar is just the bar and spans the full panel width
                    (mirrors the sync-progress fix in MainPage). */}
                <div style={{ width: "100%" }}>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: "12px",
                      marginBottom: "4px",
                    }}
                  >
                    <span
                      data-testid="dl-caption"
                      style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    >
                      {item.rom_name} ({item.platform_name}){item.status === "paused" ? " — Paused" : ""}
                    </span>
                    <span data-testid="dl-bytes" style={{ flexShrink: 0 }}>
                      {item.total_bytes > 0
                        ? `${formatBytes(item.bytes_downloaded)} / ${formatBytes(item.total_bytes)}`
                        : formatBytes(item.bytes_downloaded)}
                    </span>
                  </div>
                  <ProgressBar
                    indeterminate={item.total_bytes === 0}
                    {...(item.total_bytes > 0 ? { nProgress: (item.bytes_downloaded / item.total_bytes) * 100 } : {})}
                  />
                </div>
              </PanelSectionRow>
            ))}
            {active.map((item) => (
              <Fragment key={`actions-${item.rom_id}`}>
                {item.status === "downloading" && item.resumable && (
                  <PanelSectionRow>
                    <ButtonItem
                      layout="below"
                      onClick={() => {
                        detach(handlePause(item.rom_id));
                      }}
                    >
                      Pause {item.rom_name}
                    </ButtonItem>
                  </PanelSectionRow>
                )}
                {item.status === "paused" && (
                  <PanelSectionRow>
                    <ButtonItem
                      layout="below"
                      onClick={() => {
                        detach(handleResume(item.rom_id));
                      }}
                    >
                      Resume {item.rom_name}
                    </ButtonItem>
                  </PanelSectionRow>
                )}
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={() => {
                      detach(handleCancel(item.rom_id));
                    }}
                  >
                    Cancel {item.rom_name}
                  </ButtonItem>
                </PanelSectionRow>
              </Fragment>
            ))}

            {finished.map((item) => (
              <PanelSectionRow key={item.rom_id}>
                <Field label={item.rom_name} description={formatFinishedDescription(item)} />
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
