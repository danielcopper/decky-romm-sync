import { FC } from "react";
import { ModalRoot, DialogButton } from "@decky/ui";
import { showModal } from "@decky/ui";
import { resolveConflict } from "../api/backend";
import type { PendingConflict } from "../types";

export type ConflictResolution = "use_local" | "use_server" | "skip" | "launch_anyway" | "cancel";

interface ConflictModalProps {
  conflicts: PendingConflict[];
  closeModal?: () => void;
  onDone: (resolution: ConflictResolution) => void;
}

function formatBytes(bytes: number | null): string {
  if (bytes == null || bytes === 0) return "unknown";
  const k = 1024;
  const sizes = ["B", "KB", "MB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "unknown";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function getSystemFromPath(path: string | null): string {
  if (!path) return "";
  // Extract system from saves path: .../saves/{system}/{filename}
  const parts = path.replace(/\\/g, "/").split("/");
  const savesIdx = parts.lastIndexOf("saves");
  if (savesIdx >= 0 && savesIdx + 1 < parts.length - 1) {
    return parts[savesIdx + 1].toUpperCase();
  }
  return "";
}

const ConflictModalContent: FC<ConflictModalProps> = ({ conflicts, closeModal, onDone }) => {
  const conflict = conflicts[0];
  const remaining = conflicts.length - 1;
  const system = getSystemFromPath(conflict.local_path);

  const handleChoice = async (resolution: ConflictResolution) => {
    if (resolution === "use_local") {
      try {
        await resolveConflict(conflict.rom_id, conflict.filename, "upload");
      } catch (e) {
        console.error("[RomM] Failed to resolve conflict (upload):", e);
      }
    } else if (resolution === "use_server") {
      try {
        await resolveConflict(conflict.rom_id, conflict.filename, "download");
      } catch (e) {
        console.error("[RomM] Failed to resolve conflict (download):", e);
      }
    }
    // "skip" and "launch_anyway" leave the conflict unresolved
    closeModal?.();
    onDone(resolution);
  };

  return (
    <ModalRoot closeModal={() => { closeModal?.(); onDone("cancel"); }}>
      <div style={{ padding: "16px", minWidth: "320px" }}>
        <div style={{
          fontSize: "16px",
          fontWeight: "bold",
          marginBottom: "4px",
          color: "#fff",
        }}>
          Save Conflict Detected
        </div>
        <div style={{
          fontSize: "13px",
          color: "rgba(255, 255, 255, 0.6)",
          marginBottom: "16px",
        }}>
          {conflict.filename}
          {system && ` \u2014 ${system}`}
          {remaining > 0 && ` (+${remaining} more)`}
        </div>

        <div style={{
          display: "flex",
          gap: "12px",
          marginBottom: "16px",
        }}>
          <div style={{
            flex: 1,
            padding: "10px",
            background: "rgba(76, 175, 80, 0.15)",
            borderRadius: "4px",
            border: "1px solid rgba(76, 175, 80, 0.3)",
          }}>
            <div style={{ fontSize: "12px", fontWeight: "bold", color: "#81c784", marginBottom: "6px" }}>
              Local Save
            </div>
            <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" }}>
              {formatBytes(conflict.local_size)}
            </div>
            <div style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.4)" }}>
              {formatTimestamp(conflict.local_mtime)}
            </div>
          </div>

          <div style={{
            flex: 1,
            padding: "10px",
            background: "rgba(33, 150, 243, 0.15)",
            borderRadius: "4px",
            border: "1px solid rgba(33, 150, 243, 0.3)",
          }}>
            <div style={{ fontSize: "12px", fontWeight: "bold", color: "#64b5f6", marginBottom: "6px" }}>
              Server Save
            </div>
            <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" }}>
              {formatBytes(conflict.server_size)}
            </div>
            <div style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.4)" }}>
              {formatTimestamp(conflict.server_updated_at)}
            </div>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => handleChoice("use_local")}>
            Keep Local
          </DialogButton>
          <DialogButton onClick={() => handleChoice("use_server")}>
            Keep Server
          </DialogButton>
          {/* TODO: Enable when RomM 4.7+ save slots are available
          <DialogButton onClick={() => handleChoice("keep_both")}>
            Keep Both
          </DialogButton>
          */}
          <DialogButton
            onClick={() => handleChoice("skip")}
            style={{ opacity: 0.7 }}
          >
            Skip
          </DialogButton>
          <DialogButton
            onClick={() => handleChoice("launch_anyway")}
            style={{ opacity: 0.7 }}
          >
            Launch Anyway
          </DialogButton>
          <DialogButton
            onClick={() => handleChoice("cancel")}
            style={{ opacity: 0.5 }}
          >
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

/**
 * Show the conflict resolution modal and return a Promise that resolves
 * when the user picks an option. Used by sessionManager during pre-launch sync
 * and by the CustomPlayButton on the game detail page.
 */
export function showConflictResolutionModal(
  conflicts: PendingConflict[],
): Promise<ConflictResolution> {
  return new Promise<ConflictResolution>((resolve) => {
    showModal(
      <ConflictModalContent
        conflicts={conflicts}
        onDone={resolve}
      />,
    );
  });
}
