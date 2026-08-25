import { useEffect, FC, Fragment } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, Field } from "@decky/ui";
import { showToast } from "../utils/toast";
import {
  getDownloadQueue,
  cancelDownload,
  pauseDownload,
  resumeDownload,
  clearCompletedDownloads,
} from "../api/backend";
import { setDownloads, removeTerminalDownloads, useDownloads } from "../utils/downloadStore";
import { formatBytes } from "../utils/formatters";
import { scrollToTop } from "../utils/scrollHelpers";
import { detach } from "../utils/detach";
import { DownloadProgressRow } from "./DownloadProgressRow";
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
  const downloads = useDownloads();

  useEffect(() => {
    // Seed the store from the backend queue on mount; the subscription renders
    // whatever lands there. A rejected fetch needs no handling of its own — the
    // store keeps whatever the event listeners have already put in it, and that
    // is exactly what stays on screen.
    detach(
      getDownloadQueue().then((result) => {
        setDownloads(result.downloads);
      }),
    );
  }, []);

  const handleCancel = async (romId: number) => {
    // A successful cancel (running OR paused) drops the row via the backend's
    // terminal cancelled frame → store listener. A cancel that could not act
    // (the entry vanished between render and click) returns the failure shape —
    // surface it so the click is never a silent no-op (#149 downloads-round).
    try {
      const result = await cancelDownload(romId);
      if (!result.success) {
        showToast(result.message || "Could not cancel the download");
      }
    } catch {
      showToast("Could not cancel the download");
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

  const handleClearCompleted = async () => {
    try {
      await clearCompletedDownloads();
    } catch {
      // Clear failed (bridge/backend error) — leave the finished rows in place
      // so the list stays honest rather than hiding entries the backend kept.
      return;
    }
    // The backend evicted the terminal entries; drop them from the store too so
    // this view and any remount reflect the cleared queue immediately (#149).
    removeTerminalDownloads();
  };

  // Paused and extracting downloads stay in the active section — they're not
  // finished. Paused holds the partial transfer for resume; extracting is the
  // post-transfer ZIP unpack (not cancellable, no pause/resume offered).
  const active = downloads.filter(
    (d) => d.status === "queued" || d.status === "downloading" || d.status === "paused" || d.status === "extracting",
  );
  const finished = downloads.filter(
    (d) => d.status === "completed" || d.status === "failed" || d.status === "cancelled",
  );
  const hasFinished = finished.length > 0;

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
        {downloads.length === 0 ? (
          <PanelSectionRow>
            <Field label="No downloads" />
          </PanelSectionRow>
        ) : (
          <>
            {active.map((item) => (
              <DownloadProgressRow
                key={item.rom_id}
                caption={
                  <>
                    {item.rom_name} ({item.platform_name}){item.status === "paused" ? " — Paused" : ""}
                    {item.status === "extracting" ? " — Extracting…" : ""}
                  </>
                }
                bytesDownloaded={item.bytes_downloaded}
                totalBytes={item.total_bytes}
              />
            ))}
            {active.map((item) =>
              // Extraction is not cancellable and can't pause/resume — the
              // post-transfer unpack runs to completion. Offer no action row,
              // matching the game-detail button's disabled throbber.
              item.status === "extracting" ? null : (
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
              ),
            )}

            {finished.map((item) => (
              <PanelSectionRow key={item.rom_id}>
                <Field label={item.rom_name} description={formatFinishedDescription(item)} />
              </PanelSectionRow>
            ))}

            {hasFinished && (
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  onClick={() => {
                    detach(handleClearCompleted());
                  }}
                >
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
