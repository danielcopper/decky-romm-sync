import { FC, ReactNode } from "react";
import { PanelSectionRow, ProgressBar } from "@decky/ui";
import { formatBytes } from "../utils/formatters";
import { wrapText } from "../utils/textStyles";

interface DownloadProgressRowProps {
  /** The row's caption — a bare ROM name on the QAM summary, or a composed
   *  "name (platform) — Paused" node on the full queue. Whatever the caller
   *  passes renders inside the `dl-caption` span. */
  caption: ReactNode;
  bytesDownloaded: number;
  totalBytes: number;
}

/**
 * One active-download row: a full-width caption + byte-count header above a bare
 * ProgressBar. Shared by the QAM main-page download summary and the full download
 * queue so both surfaces render byte-identically.
 *
 * The bare ProgressBar (not ProgressBarWithInfo) spans the full panel width:
 * ProgressBarWithInfo is a Steam Field (label column | bar column) and with the
 * caption in the label column the empty bar column gets squeezed into the right
 * half and clips (#751). The caption wraps to as many lines as needed instead of
 * clipping (shared wrap rule); the bytes column stays pinned top-right. An unknown
 * total (`totalBytes === 0`) leaves the bar indeterminate and shows the downloaded
 * bytes alone.
 */
export const DownloadProgressRow: FC<DownloadProgressRowProps> = ({ caption, bytesDownloaded, totalBytes }) => (
  <PanelSectionRow>
    <div style={{ width: "100%" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          fontSize: "12px",
          marginBottom: "4px",
        }}
      >
        <span data-testid="dl-caption" style={wrapText}>
          {caption}
        </span>
        <span data-testid="dl-bytes" style={{ flexShrink: 0 }}>
          {totalBytes > 0
            ? `${formatBytes(bytesDownloaded)} / ${formatBytes(totalBytes)}`
            : formatBytes(bytesDownloaded)}
        </span>
      </div>
      <ProgressBar
        indeterminate={totalBytes === 0}
        {...(totalBytes > 0 ? { nProgress: (bytesDownloaded / totalBytes) * 100 } : {})}
      />
    </div>
  </PanelSectionRow>
);
