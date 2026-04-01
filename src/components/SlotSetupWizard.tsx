import { useState, useEffect, FC, createElement, ChangeEvent } from "react";
import { DialogButton, ConfirmModal, TextField, showModal } from "@decky/ui";
import { getSaveSetupInfo, confirmSlotChoice, logError } from "../api/backend";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import type { SaveSetupInfo } from "../types";

interface SlotSetupWizardProps {
  romId: number;
  onComplete: () => void;
}

function displaySlot(slot: string | null): string {
  if (slot === null || slot === "") return "(no slot)";
  return slot;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

// Compact button styling — white text, subtle border, small
const btnStyle: React.CSSProperties = {
  background: "transparent",
  border: "1px solid rgba(255, 255, 255, 0.3)",
  borderRadius: "4px",
  padding: "4px 12px",
  minWidth: "auto",
  width: "auto",
  fontSize: "12px",
  color: "#fff",
  cursor: "pointer",
};

const btnPrimaryStyle: React.CSSProperties = {
  ...btnStyle,
  background: "rgba(26, 159, 255, 0.15)",
  border: "1px solid rgba(26, 159, 255, 0.4)",
  color: "#1a9fff",
};

function getWizardDescription(info: SaveSetupInfo): string {
  if (!info.has_local_saves && info.server_slots.length > 0) {
    return "Server has saves \u2014 choose which slot to track.";
  }
  if (info.has_local_saves && info.server_slots.length > 0) {
    return "You have local saves and the server has saves too.";
  }
  return "Choose a save slot to get started.";
}

export const SlotSetupWizard: FC<SlotSetupWizardProps> = ({ romId, onComplete }) => {
  const [info, setInfo] = useState<SaveSetupInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [customSlot, setCustomSlot] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const autoConfirmIfNeeded = async (result: SaveSetupInfo): Promise<boolean> => {
      if (!result.has_local_saves || result.server_slots.length > 0) return false;
      setConfirming(true);
      try {
        await confirmSlotChoice(romId, result.default_slot, null);
        if (!cancelled) onComplete();
      } catch (e) {
        if (!cancelled) {
          setError(`Auto-setup failed: ${e}`);
          logError(`SlotSetupWizard auto-confirm failed: ${e}`);
          setConfirming(false);
          setInfo(result);
        }
      }
      return true;
    };

    const fetchInfo = async () => {
      setLoading(true);
      setError(null);
      try {
        const result = await getSaveSetupInfo(romId);
        if (cancelled) return;

        const autoConfirmed = await autoConfirmIfNeeded(result);
        if (autoConfirmed) {
          if (!cancelled) setLoading(false);
          return;
        }

        setInfo(result);
      } catch (e) {
        if (!cancelled) {
          setError(`Failed to load save setup info: ${e}`);
          logError(`SlotSetupWizard fetch failed: ${e}`);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchInfo();
    return () => { cancelled = true; };
  }, [romId]);

  const handleConfirm = async (slot: string) => {
    setConfirming(true);
    setError(null);
    try {
      // No migration — just set the slot. User must explicitly choose migration later.
      const result = await confirmSlotChoice(romId, slot, null);
      if (!result.success) {
        setError(result.message || "Slot confirmation failed");
        setConfirming(false);
        return;
      }
      onComplete();
    } catch (e) {
      setError(`Failed to confirm slot: ${e}`);
      logError(`SlotSetupWizard confirm failed: ${e}`);
      setConfirming(false);
    }
  };

  // Loading / confirming
  if (loading || (confirming && !error)) {
    return (
      <div style={{ padding: "12px 0" }}>
        <div className="romm-panel-section-title">Save Slot Setup</div>
        <div className="romm-panel-muted">
          {confirming ? "Setting up..." : "Loading save information..."}
        </div>
      </div>
    );
  }

  // Error without data — show retry
  if (error && !info) {
    return (
      <div style={{ padding: "12px 0" }}>
        <div className="romm-panel-section-title">Save Slot Setup</div>
        <div style={{ color: "#d4513f", fontSize: "12px", marginBottom: "8px" }}>{error}</div>
        <DialogButton
          style={btnStyle}
          onClick={() => {
            setError(null);
            setLoading(true);
            getSaveSetupInfo(romId).then(
              (result) => { setInfo(result); setLoading(false); },
              (e) => { setError(`Failed: ${e}`); setLoading(false); },
            );
          }}
        >
          Retry
        </DialogButton>
      </div>
    );
  }

  if (!info) return null;

  const defaultSlot = info.default_slot;

  // ── Two-column layout ──────────────────────────────────────

  // Left column: local saves info
  const leftChildren: React.ReactNode[] = [];
  leftChildren.push(
    <div key="local-title" className="romm-panel-section-title" style={{ marginBottom: "8px" }}>
      Local Saves
    </div>,
  );

  if (info.local_files.length > 0) {
    leftChildren.push(
      <div key="local-files">
        {info.local_files.map((f) => (
          <div
            key={f.filename}
            style={{ display: "flex", alignItems: "center", gap: "6px", padding: "4px 0", fontSize: "12px" }}
          >
            <span className="romm-status-dot" style={{ backgroundColor: "#5ba32b" }} />
            <span style={{ color: "#fff" }}>{f.filename}</span>
            <span className="romm-panel-muted">{formatSize(f.size)}</span>
          </div>
        ))}
      </div>,
    );
  } else {
    leftChildren.push(
      <div key="no-local" className="romm-panel-muted" style={{ fontSize: "12px" }}>
        No local saves found
      </div>,
    );
  }

  // Right column: server slots + actions
  const rightChildren: React.ReactNode[] = [];
  rightChildren.push(
    <div key="server-title" className="romm-panel-section-title" style={{ marginBottom: "8px" }}>
      Server Slots
    </div>,
  );

  if (info.server_slots.length > 0) {
    info.server_slots.forEach((s) => {
      const slotKey = s.slot ?? "__null__";
      rightChildren.push(
        <div
          key={`slot-${slotKey}`}
          style={{
            padding: "6px 0",
            borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "8px",
          }}
        >
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "13px", color: "#fff" }}>
              <span className="romm-status-dot" style={{ backgroundColor: "#1a9fff" }} />
              {displaySlot(s.slot)}
            </div>
            <div className="romm-panel-muted" style={{ fontSize: "11px", marginLeft: "18px" }}>
              {s.count} file{s.count === 1 ? "" : "s"}
              {s.latest_updated_at ? ` \u2014 ${formatTimestamp(s.latest_updated_at)}` : ""}
            </div>
          </div>
          <DialogButton
            style={btnStyle}
            disabled={confirming}
            onClick={() => handleConfirm(s.slot ?? defaultSlot)}
            {...{ onFocus: scrollFocusedToCenter } as any}
          >
            Track
          </DialogButton>
        </div>,
      );
    });
  } else {
    rightChildren.push(
      <div key="no-server" className="romm-panel-muted" style={{ fontSize: "12px" }}>
        No saves on server
      </div>,
    );
  }

  // Divider + "Start fresh" section — only show "Use default" when it's not already in the server list
  const defaultExistsOnServer = info.server_slots.some((s) => s.slot === defaultSlot);
  rightChildren.push(
    <div key="divider" style={{ borderTop: "1px solid rgba(255, 255, 255, 0.1)", margin: "10px 0 8px" }} />,
  );
  if (!defaultExistsOnServer) {
    rightChildren.push(
      <div key="fresh-label" className="romm-panel-muted" style={{ fontSize: "11px", marginBottom: "6px" }}>
        Or start fresh:
      </div>,
      <div key="default-btn" style={{ marginBottom: "6px" }}>
        <DialogButton
          style={btnPrimaryStyle}
          disabled={confirming}
          onClick={() => handleConfirm(defaultSlot)}
          {...{ onFocus: scrollFocusedToCenter } as any}
        >
          Use slot &lsquo;{defaultSlot}&rsquo;
        </DialogButton>
      </div>,
    );
  }

  rightChildren.push(
    <div key="custom-toggle">
      <DialogButton
        style={btnStyle}
        disabled={confirming}
        {...{ onFocus: scrollFocusedToCenter } as any}
        onClick={() => {
          showModal(
            createElement(ConfirmModal, {
              strTitle: "Custom Slot Name",
              bDisableBackgroundDismiss: true,
              onOK: () => {
                const trimmed = customSlot.trim();
                if (trimmed) {
                  handleConfirm(trimmed);
                } else {
                  // Legacy mode
                  showModal(
                    createElement(ConfirmModal, {
                      strTitle: "Use Legacy Mode?",
                      strDescription: "Legacy mode (no slot) limits saves to one version per game. Are you sure?",
                      onOK: () => handleConfirm(""),
                    }),
                  );
                }
              },
            },
              createElement(TextField, {
                focusOnMount: true,
                label: "Slot Name",
                value: customSlot,
                onChange: (e: ChangeEvent<HTMLInputElement>) => setCustomSlot(e.target.value),
              } as any),
            ),
          );
        }}
      >
        Custom slot...
      </DialogButton>
    </div>,
  );

  return (
    <div style={{ padding: "12px 0" }}>
      <div className="romm-panel-section-title" style={{ marginBottom: "4px" }}>Save Slot Setup</div>
      {error && (
        <div style={{ color: "#d4513f", fontSize: "12px", marginBottom: "8px" }}>{error}</div>
      )}
      <div className="romm-panel-muted" style={{ fontSize: "12px", marginBottom: "12px" }}>
        {getWizardDescription(info)}
      </div>
      <div style={{ display: "flex", gap: "24px" }}>
        <div style={{ flex: 2, minWidth: 0 }}>{leftChildren}</div>
        <div style={{ flex: 1, minWidth: 0 }}>{rightChildren}</div>
      </div>
    </div>
  );
};
