import { useState, useEffect, useRef, FC, createElement, ChangeEvent } from "react";
import { DialogButton, ConfirmModal, TextField, showModal } from "@decky/ui";
import { getSaveSetupInfo, confirmSlotChoice, logError } from "../api/backend";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import {
  applyWizardInitialSetupResult,
  applyWizardRetrySetupResult,
  SERVER_UNREACHABLE_WIZARD_MESSAGE,
} from "../utils/saveSetup";
import {
  getRommConnectionState,
  onRommConnectionChange,
  reportServerReachable,
  setServerRetryProgress,
} from "../utils/connectionState";
import { formatBytes } from "../utils/formatters";
import type { SaveSetupInfo } from "../types";
import { detach } from "../utils/detach";
import { ConnectingIndicator } from "./saves/ConnectingIndicator";

interface SlotSetupWizardProps {
  romId: number;
  onComplete: () => void;
}

function displaySlot(slot: string | null): string {
  if (slot === null || slot === "") return "Legacy (no slot)";
  return slot;
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

const CustomSlotModal: FC<{
  closeModal?: () => void;
  onSubmit: (name: string) => void;
}> = ({ closeModal, onSubmit }) => {
  const [value, setValue] = useState("");
  return createElement(
    ConfirmModal,
    {
      ...(closeModal !== undefined ? { closeModal } : {}),
      strTitle: "Custom Slot Name",
      bDisableBackgroundDismiss: true,
      onOK: () => {
        onSubmit(value.trim());
      },
    },
    createElement(TextField, {
      focusOnMount: true,
      label: "Slot Name",
      value,
      onChange: (e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value),
    }),
  );
};

export const SlotSetupWizard: FC<SlotSetupWizardProps> = ({ romId, onComplete }) => {
  const [info, setInfo] = useState<SaveSetupInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped to force a re-fetch — the reconnect auto-reload uses it to re-run the
  // load effect once the shared connection store flips back to connected (#1345).
  const [reloadKey, setReloadKey] = useState(0);
  // True while the wizard is holding on the offline error (fast path or a
  // server_unreachable result). Gates the reconnect auto-reload so a benign
  // checking→connected transition never re-fetches, and guards against loops.
  const offlineHeldRef = useRef(false);

  useEffect(() => {
    let cancelled = false;

    const fetchInfo = async () => {
      // Known-offline fast path (#1345): getSaveSetupInfo runs the backend
      // retry+backoff ladder, so on a known-unreachable server every saves-tab
      // re-open would hang "Connecting to RomM…" for ~13s before falling back to
      // the retry view. Skip the call and render the unreachable state instantly;
      // offlineHeldRef arms the reconnect auto-reload below.
      if (getRommConnectionState() === "offline") {
        offlineHeldRef.current = true;
        if (!cancelled) {
          setError(SERVER_UNREACHABLE_WIZARD_MESSAGE);
          setLoading(false);
        }
        return;
      }

      // Clear any stale retry progress from a previous load before starting a
      // fresh one, so ConnectingIndicator shows plain "Connecting to RomM…" and
      // not a leftover "(attempt N/M)" (#1345 round-2 review). Clear-on-start is
      // race-free — a clear-on-complete could wipe a still-live retry frame.
      setServerRetryProgress(null);
      setLoading(true);
      setError(null);
      try {
        const result = await getSaveSetupInfo(romId);
        if (cancelled) return;
        // Feed the shared store (#1345): a server_unreachable result is a
        // definitive offline signal; any other resolved result proves the
        // server answered. A throw is a bridge/unknown error, not a verdict —
        // the catch leaves the store untouched.
        const reachable = result.recommended_action !== "server_unreachable";
        reportServerReachable(reachable);
        offlineHeldRef.current = !reachable;
        await applyWizardInitialSetupResult(result, {
          romId,
          confirmSlotChoice,
          setError,
          setConfirming,
          setInfo,
          logError,
          onComplete,
          isCancelled: () => cancelled,
        });
      } catch (e) {
        if (!cancelled) {
          setError(`Failed to load save setup info: ${e}`);
          logError(`SlotSetupWizard fetch failed: ${e}`);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    detach(fetchInfo());
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onComplete is a fresh arrow every parent render; including it would re-fetch on every render. reloadKey is the explicit reconnect re-fetch trigger.
  }, [romId, reloadKey]);

  // Auto-reload on reconnect (#1345): when the shared store flips back to
  // connected while the wizard is holding the offline error, re-run the load.
  // Bounded — fires only on the →connected edge and only when armed, so a
  // still-unreachable re-fetch (which re-arms via reportServerReachable(false))
  // can't loop faster than the 30s recovery probe that flips the store.
  useEffect(
    () =>
      onRommConnectionChange((s) => {
        if (s === "connected" && offlineHeldRef.current) {
          offlineHeldRef.current = false;
          setReloadKey((k) => k + 1);
        }
      }),
    [],
  );

  const handleConfirm = async (slot: string, migrate = false, migrateFrom: string | null = null) => {
    setConfirming(true);
    setError(null);
    try {
      // Confirm a non-empty named slot (legacy slot:null is retired, #1276).
      // Defaults are the non-destructive path (migrate=false, from=null); the
      // legacy-group Track button passes migrate=true, from=null to carry the
      // legacy saves into the target slot.
      const result = await confirmSlotChoice(romId, slot, migrate, migrateFrom);
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

  // Loading / confirming — a spinner + live retry progress (#1345) instead of
  // bare italic text, so a load paying the backend retry ladder reads as busy.
  if (loading || (confirming && !error)) {
    return (
      <div style={{ padding: "12px 0" }}>
        <div className="romm-panel-section-title">Save Slot Setup</div>
        <ConnectingIndicator label={confirming ? "Setting up" : "Connecting to RomM"} />
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
          className="romm-wizard-btn"
          style={btnStyle}
          onClick={() => {
            setError(null);
            setLoading(true);
            // Fresh load — drop any stale retry progress (see fetchInfo above).
            setServerRetryProgress(null);
            getSaveSetupInfo(romId).then(
              (result) => {
                // Same conservative feed as the initial load (#1345): the manual
                // Retry re-probes reachability, so a server_unreachable result
                // re-arms offline and any other result reports the server back.
                const reachable = result.recommended_action !== "server_unreachable";
                reportServerReachable(reachable);
                offlineHeldRef.current = !reachable;
                applyWizardRetrySetupResult(result, { setError, setLoading, setInfo });
              },
              (e) => {
                setError(`Failed: ${e}`);
                setLoading(false);
              },
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
            <span className="romm-panel-muted">{formatBytes(f.size)}</span>
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
      const isLegacyGroup = s.slot === null || s.slot === "";
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
            className="romm-wizard-btn"
            style={btnStyle}
            onClick={() => {
              if (isLegacyGroup) {
                // Legacy (no-slot) saves can no longer be tracked as-is (#1276).
                // Offer to migrate them into the default slot rather than
                // confirming the retired legacy mode.
                showModal(
                  createElement(ConfirmModal, {
                    strTitle: "Migrate Legacy Saves?",
                    strDescription: `Migrate legacy saves into ‘${defaultSlot}’?`,
                    onOK: () => {
                      detach(handleConfirm(defaultSlot, true, null));
                    },
                  }),
                );
              } else {
                detach(handleConfirm(s.slot as string));
              }
            }}
            onFocus={scrollFocusedToCenter}
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
          className="romm-wizard-btn romm-wizard-btn-primary"
          style={btnPrimaryStyle}
          onClick={() => {
            detach(handleConfirm(defaultSlot));
          }}
          onFocus={scrollFocusedToCenter}
        >
          Use slot &lsquo;{defaultSlot}&rsquo;
        </DialogButton>
      </div>,
    );
  }

  rightChildren.push(
    <div key="custom-toggle">
      <DialogButton
        className="romm-wizard-btn"
        style={btnStyle}
        onFocus={scrollFocusedToCenter}
        onClick={() => {
          showModal(
            createElement(CustomSlotModal, {
              onSubmit: (trimmed: string) => {
                // An empty custom name is rejected by the backend's
                // invalid_slot_name guard — never reinterpret it as the retired
                // legacy no-slot mode (#1276).
                detach(handleConfirm(trimmed));
              },
            }),
          );
        }}
      >
        Custom slot...
      </DialogButton>
    </div>,
  );

  return (
    <div style={{ padding: "12px 0" }}>
      <div className="romm-panel-section-title" style={{ marginBottom: "4px" }}>
        Save Slot Setup
      </div>
      {error && <div style={{ color: "#d4513f", fontSize: "12px", marginBottom: "8px" }}>{error}</div>}
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
