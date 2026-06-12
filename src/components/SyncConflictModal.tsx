import { FC, useState } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import { resolveSyncConflict, logError } from "../api/backend";
import type { SyncConflict } from "../types";
import { formatBytes, formatTimestamp } from "../utils/formatters";

type SyncConflictAction = "keep_local" | "use_server";
export type SyncConflictResolution = SyncConflictAction | "cancel";

interface SyncConflictModalProps {
  conflict: SyncConflict;
  onResolve: (action: SyncConflictAction) => Promise<void>;
  onCancel: () => void;
  isLoading?: boolean;
  errorMessage?: string | null;
}

/** "unknown" when bytes is null or 0 — otherwise the shared byte formatter output. */
function formatSize(bytes: number | null): string {
  if (bytes == null || bytes === 0) return "unknown";
  return formatBytes(bytes);
}

/**
 * Controlled modal: parent owns isLoading + errorMessage. Three actions:
 *   - Keep Local  -> onResolve("keep_local")  -> backend PUTs local to server
 *   - Use Server  -> onResolve("use_server")  -> backend downloads server, overwrites local
 *   - Cancel      -> onCancel()               -> pure UI close, no backend call.
 *                                                Conflict re-fires on next sync if state still holds.
 *
 * If `onResolve` throws, the parent should set `errorMessage` and keep the modal
 * mounted so the user can retry. Buttons are disabled while `isLoading` is true.
 */
const SyncConflictModal: FC<SyncConflictModalProps> = ({
  conflict,
  onResolve,
  onCancel,
  isLoading = false,
  errorMessage = null,
}) => {
  const handleResolve = (action: SyncConflictAction) => {
    onResolve(action).catch(() => {
      // onResolve owns its own error handling; swallow rejections at the
      // event-handler boundary so React doesn't see an unhandled promise.
    });
  };

  return (
    <ModalRoot closeModal={isLoading ? undefined : onCancel}>
      <div style={{ padding: "16px", minWidth: "360px" }}>
        <div
          style={{
            fontSize: "16px",
            fontWeight: "bold",
            marginBottom: "4px",
            color: "#fff",
          }}
        >
          Save conflict for {conflict.filename}
        </div>
        <div
          style={{
            fontSize: "12px",
            color: "rgba(255, 255, 255, 0.6)",
            marginBottom: "16px",
            lineHeight: "1.4",
          }}
        >
          Both your local save and the server save have changed since the last sync. Pick which version to keep — the
          other will be overwritten.
        </div>

        {/* Local save block */}
        <div
          style={{
            padding: "10px",
            background: "rgba(76, 175, 80, 0.15)",
            borderRadius: "4px",
            border: "1px solid rgba(76, 175, 80, 0.3)",
            marginBottom: "10px",
          }}
        >
          <div style={{ fontSize: "12px", fontWeight: "bold", color: "#81c784", marginBottom: "6px" }}>
            Your local save
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "2px" }}>
            {formatSize(conflict.local_size)} · modified {formatTimestamp(conflict.local_mtime)}
          </div>
          <div style={{ marginTop: "8px" }}>
            <DialogButton onClick={() => handleResolve("keep_local")} disabled={isLoading}>
              Keep Local
            </DialogButton>
          </div>
        </div>

        {/* Server save block */}
        <div
          style={{
            padding: "10px",
            background: "rgba(33, 150, 243, 0.15)",
            borderRadius: "4px",
            border: "1px solid rgba(33, 150, 243, 0.3)",
            marginBottom: "10px",
          }}
        >
          <div style={{ fontSize: "12px", fontWeight: "bold", color: "#64b5f6", marginBottom: "6px" }}>
            Server save (id={conflict.server_save_id})
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "2px" }}>
            {formatSize(conflict.server_size)} · uploaded {formatTimestamp(conflict.server_updated_at)}
          </div>
          <div style={{ marginTop: "8px" }}>
            <DialogButton onClick={() => handleResolve("use_server")} disabled={isLoading}>
              Use Server
            </DialogButton>
          </div>
        </div>

        {errorMessage ? (
          <div
            style={{
              padding: "8px 10px",
              background: "rgba(244, 67, 54, 0.15)",
              borderRadius: "4px",
              border: "1px solid rgba(244, 67, 54, 0.3)",
              marginBottom: "12px",
              fontSize: "12px",
              color: "#ef9a9a",
              lineHeight: "1.4",
            }}
          >
            {errorMessage}
          </div>
        ) : null}

        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <DialogButton onClick={onCancel} disabled={isLoading} style={{ opacity: 0.7 }}>
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

interface SyncConflictModalHostProps {
  conflict: SyncConflict;
  closeModal?: () => void;
  onDone: (resolution: SyncConflictResolution) => void;
}

/**
 * Stateful wrapper: handles the resolveSyncConflict callable, error display,
 * and modal close timing. Used by `showSyncConflictModal`.
 */
const SyncConflictModalHost: FC<SyncConflictModalHostProps> = ({ conflict, closeModal, onDone }) => {
  const [isLoading, setIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const handleResolve = async (action: SyncConflictAction): Promise<void> => {
    setIsLoading(true);
    setErrorMessage(null);
    try {
      const result = await resolveSyncConflict(conflict.rom_id, conflict.filename, conflict.server_save_id, action);
      if (!result.success) {
        if (result.reason === "stale_conflict") {
          const staleMsg =
            "The server save has been updated by another device. Please cancel and retry sync to get the latest version.";
          logError(
            `resolveSyncConflict(${conflict.rom_id}, ${conflict.filename}, ${action}) stale: ${result.message ?? ""}`,
          );
          setErrorMessage(staleMsg);
          setIsLoading(false);
          return;
        }
        const msg = result.message ?? "Failed to resolve conflict";
        logError(`resolveSyncConflict(${conflict.rom_id}, ${conflict.filename}, ${action}) failed: ${msg}`);
        setErrorMessage(msg);
        setIsLoading(false);
        return;
      }
      closeModal?.();
      onDone(action);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      logError(`resolveSyncConflict(${conflict.rom_id}, ${conflict.filename}, ${action}) threw: ${msg}`);
      setErrorMessage(msg || "Failed to resolve conflict");
      setIsLoading(false);
    }
  };

  const handleCancel = () => {
    if (isLoading) return;
    closeModal?.();
    onDone("cancel");
  };

  return (
    <SyncConflictModal
      conflict={conflict}
      onResolve={handleResolve}
      onCancel={handleCancel}
      isLoading={isLoading}
      errorMessage={errorMessage}
    />
  );
};

/**
 * Show the sync-conflict modal and return a Promise that resolves once the
 * user picks an action (or cancels). Used by CustomPlayButton during pre-launch
 * sync and by sessionManager when post-exit sync surfaces conflicts.
 */
export function showSyncConflictModal(conflict: SyncConflict): Promise<SyncConflictResolution> {
  return new Promise<SyncConflictResolution>((resolve) => {
    showModal(<SyncConflictModalHost conflict={conflict} onDone={resolve} />);
  });
}
