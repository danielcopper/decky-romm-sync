import { useState, useEffect, useRef, FC, createElement, ChangeEvent } from "react";
import { toaster } from "@decky/api";
import { DialogButton, ConfirmModal, ModalRoot, TextField, showModal } from "@decky/ui";
import { getSaveSetupInfo, confirmSlotChoice, logError } from "../api/backend";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import {
  applyWizardInitialSetupResult,
  applyWizardRetrySetupResult,
  legacyConflictReplaceNotice,
  legacyMigrateConfirmDescription,
  legacyTrackExplainer,
  startFreshHint,
  startFreshHintNewSlot,
  wizardMigrationOutcomeToastBody,
  SERVER_UNREACHABLE_WIZARD_MESSAGE,
} from "../utils/saveSetup";
import {
  getRommConnectionState,
  onRommConnectionChange,
  reportServerReachable,
  setServerRetryProgress,
} from "../utils/connectionState";
import { formatBytes } from "../utils/formatters";
import type { SaveSetupInfo, SlotMigrationConflict } from "../types";
import { detach } from "../utils/detach";
import { ConnectingIndicator } from "./saves/ConnectingIndicator";
import { displaySlot } from "./saves/helpers";

interface SlotSetupWizardProps {
  romId: number;
  onComplete: () => void;
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

/** "unknown" for a null/zero byte count; otherwise the shared byte formatter. */
function formatSize(bytes: number | null): string {
  if (bytes == null || bytes === 0) return "unknown";
  return formatBytes(bytes);
}

/** Informed confirmation for a legacy migration whose target already has a
 *  differing local save (#1498). Both sides are shown with size + timestamp —
 *  that comparison is what stops a newer local save being buried unnoticed — and
 *  the dialog offers exactly two ways out: confirm (proceed, quarantining the
 *  local file) or Cancel, which changes nothing and leaves the slot unconfirmed
 *  so the user can take the wizard's start-fresh route instead. There is no
 *  "keep my local save" action: it would produce the same end state as the
 *  wizard's own "Use slot" button. */
const LegacyMigrationConflictModal: FC<{
  closeModal?: () => void;
  conflicts: SlotMigrationConflict[];
  slot: string;
  onConfirm: () => void;
}> = ({ closeModal, conflicts, slot, onConfirm }) => {
  const confirm = () => {
    closeModal?.();
    onConfirm();
  };
  return (
    <ModalRoot {...(closeModal !== undefined ? { closeModal } : {})}>
      <div style={{ padding: "8px 4px", minWidth: "360px" }}>
        <div style={{ fontSize: "15px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          A local save differs from the legacy save
        </div>
        <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.6)", marginBottom: "12px", lineHeight: "1.4" }}>
          Copying the legacy save into &lsquo;{slot}&rsquo; replaces your local save.
        </div>
        {conflicts.map((c) => (
          <div key={c.filename} style={{ marginBottom: "10px" }}>
            <div style={{ fontSize: "12px", color: "#fff", marginBottom: "4px" }}>{c.filename}</div>
            <div style={{ display: "flex", gap: "8px" }}>
              <div
                style={{
                  flex: 1,
                  padding: "8px",
                  background: "rgba(76, 175, 80, 0.15)",
                  border: "1px solid rgba(76, 175, 80, 0.3)",
                  borderRadius: "4px",
                }}
              >
                <div style={{ fontSize: "11px", fontWeight: "bold", color: "#81c784", marginBottom: "2px" }}>
                  Your local save
                </div>
                <div style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.7)" }}>
                  {formatSize(c.local_size)} · modified {formatTimestamp(c.local_mtime)}
                </div>
              </div>
              <div
                style={{
                  flex: 1,
                  padding: "8px",
                  background: "rgba(33, 150, 243, 0.15)",
                  border: "1px solid rgba(33, 150, 243, 0.3)",
                  borderRadius: "4px",
                }}
              >
                <div style={{ fontSize: "11px", fontWeight: "bold", color: "#64b5f6", marginBottom: "2px" }}>
                  Legacy save on server
                </div>
                <div style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.7)" }}>
                  {formatSize(c.server_size)} · saved {formatTimestamp(c.server_updated_at)}
                </div>
              </div>
            </div>
          </div>
        ))}
        <div style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.5)", margin: "4px 0 12px", lineHeight: "1.4" }}>
          {legacyConflictReplaceNotice(slot)}
        </div>
        <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
          <DialogButton style={btnStyle} onClick={() => closeModal?.()} onFocus={scrollFocusedToCenter}>
            Cancel
          </DialogButton>
          <DialogButton style={btnPrimaryStyle} onClick={confirm} onFocus={scrollFocusedToCenter}>
            Replace local save
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

interface ConfirmHandlerDeps {
  romId: number;
  setConfirming: (confirming: boolean) => void;
  setError: (message: string | null) => void;
  onComplete: () => void;
}

/** The wizard's slot-confirm entry point, as every action button calls it. */
type ConfirmHandler = (
  slot: string,
  migrate?: boolean,
  migrateFrom?: string | null,
  useServerOnConflict?: boolean,
) => Promise<void>;

/** Build the wizard's slot-confirm handler.
 *
 *  Kept at module level rather than inline in the component so the whole confirm
 *  flow reads as one unit — probe → conflict dialog → failure → outcome toast —
 *  and so it can re-invoke itself for the "Replace local save" second call. The
 *  deps are supplied fresh on every render, exactly as the inline closure read
 *  them.
 */
function createConfirmHandler({ romId, setConfirming, setError, onComplete }: ConfirmHandlerDeps) {
  const handleConfirm = async (
    slot: string,
    migrate = false,
    migrateFrom: string | null = null,
    useServerOnConflict = false,
  ): Promise<void> => {
    setConfirming(true);
    setError(null);
    try {
      // Confirm a non-empty named slot (legacy slot:null is retired, #1276).
      // Defaults are the non-destructive path (migrate=false, from=null); the
      // legacy-group Track button passes migrate=true, from=null to copy the
      // legacy saves into the target slot (#1498).
      const result = await confirmSlotChoice(romId, slot, migrate, migrateFrom, useServerOnConflict);

      // A content-based migration found a differing local save — nothing was
      // confirmed. Show the comparison and let the user confirm the replacement;
      // cancelling makes no second call, so the slot stays unconfirmed and the
      // wizard's start-fresh route is still open.
      if (result.needs_conflict_resolution) {
        setConfirming(false);
        showModal(
          createElement(LegacyMigrationConflictModal, {
            conflicts: result.conflicts ?? [],
            slot,
            onConfirm: () => detach(handleConfirm(slot, true, migrateFrom, true)),
          }),
        );
        return;
      }

      if (!result.success) {
        setError(result.message || "Slot confirmation failed");
        setConfirming(false);
        return;
      }

      // Surface the migration outcome (names the slot + counts) so the copy is
      // never log-only. Only a migration reports counts; a plain confirm doesn't.
      if (migrate) {
        const body = wizardMigrationOutcomeToastBody(result.migrated ?? 0, result.failed ?? 0, slot);
        if (body) toaster.toast({ title: "RomM Sync", body });
      }
      onComplete();
    } catch (e) {
      setError(`Failed to confirm slot: ${e}`);
      logError(`SlotSetupWizard confirm failed: ${e}`);
      setConfirming(false);
    }
  };
  return handleConfirm;
}

/** Left column — what the device already has on disk. */
function buildLocalSavesColumn(info: SaveSetupInfo): React.ReactNode[] {
  const children: React.ReactNode[] = [
    <div key="local-title" className="romm-panel-section-title" style={{ marginBottom: "8px" }}>
      Local Saves
    </div>,
  ];

  if (info.local_files.length > 0) {
    children.push(
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
    children.push(
      <div key="no-local" className="romm-panel-muted" style={{ fontSize: "12px" }}>
        No local saves found
      </div>,
    );
  }
  return children;
}

/** One server-slot row: the slot summary plus its Track action. The legacy
 *  (slot-less) group is the special case — it carries the migration explainer and
 *  routes Track through the migrate-confirm modal instead of a plain confirm. */
function buildSlotRow(
  s: SaveSetupInfo["server_slots"][number],
  defaultSlot: string,
  handleConfirm: ConfirmHandler,
): React.ReactNode {
  const slotKey = s.slot ?? "__null__";
  const isLegacyGroup = s.slot === null || s.slot === "";
  return (
    <div
      key={`slot-${slotKey}`}
      style={{
        padding: "6px 0",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "8px",
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "13px", color: "#fff" }}>
          <span className="romm-status-dot" style={{ backgroundColor: "#1a9fff" }} />
          {displaySlot(s.slot)}
        </div>
        <div className="romm-panel-muted" style={{ fontSize: "11px", marginLeft: "18px" }}>
          {s.count} file{s.count === 1 ? "" : "s"}
          {s.latest_updated_at ? ` — ${formatTimestamp(s.latest_updated_at)}` : ""}
        </div>
        {/* Pre-click explainer so the legacy "Track" reads as a migration
            before it is clicked, not only inside the confirm modal (#1498). */}
        {isLegacyGroup ? (
          <div className="romm-panel-muted" style={{ fontSize: "11px", marginLeft: "18px", marginTop: "2px" }}>
            {legacyTrackExplainer(defaultSlot)}
          </div>
        ) : null}
      </div>
      <DialogButton
        className="romm-wizard-btn"
        style={btnStyle}
        onClick={() => {
          if (isLegacyGroup) {
            // Legacy (no-slot) saves can no longer be tracked as-is (#1276).
            // Offer to copy them into the default slot rather than confirming
            // the retired legacy mode. A differing local save is asked about
            // by the backend after OK (#1498).
            showModal(
              createElement(ConfirmModal, {
                strTitle: "Migrate Legacy Saves?",
                strDescription: legacyMigrateConfirmDescription(defaultSlot),
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
    </div>
  );
}

/** Right column — the server's slots plus the start-fresh actions. */
function buildServerSlotsColumn(
  info: SaveSetupInfo,
  defaultSlot: string,
  handleConfirm: ConfirmHandler,
): React.ReactNode[] {
  const children: React.ReactNode[] = [
    <div key="server-title" className="romm-panel-section-title" style={{ marginBottom: "8px" }}>
      Server Slots
    </div>,
  ];

  if (info.server_slots.length > 0) {
    info.server_slots.forEach((s) => children.push(buildSlotRow(s, defaultSlot, handleConfirm)));
  } else {
    children.push(
      <div key="no-server" className="romm-panel-muted" style={{ fontSize: "12px" }}>
        No saves on server
      </div>,
    );
  }

  // Divider + "Start fresh" section — only show "Use default" when it's not already in the server list
  const defaultExistsOnServer = info.server_slots.some((s) => s.slot === defaultSlot);
  children.push(
    <div key="divider" style={{ borderTop: "1px solid rgba(255, 255, 255, 0.1)", margin: "10px 0 8px" }} />,
  );
  if (!defaultExistsOnServer) {
    children.push(
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
    // A fresh slot is not empty forever: the local save is uploaded into it on
    // the next sync — spell that out so "the slot stays empty" never reads as a
    // dead end (#1478/#1498). Only meaningful when a local save exists.
    if (info.has_local_saves) {
      children.push(
        <div key="fresh-hint" className="romm-panel-muted" style={{ fontSize: "11px", marginBottom: "6px" }}>
          {startFreshHint(defaultSlot)}
        </div>,
      );
    }
  }

  children.push(
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

  // "Custom slot…" takes the same backend path as "Use slot ‘<default>’", so it
  // needs the same next-sync expectation. The named hint above already carries it
  // when the start-fresh block renders; when the default slot already exists on
  // the server that block (and its hint) is gone and only this route remains, so
  // render the slot-agnostic variant here instead — never both, and never naming
  // a slot the user isn't choosing.
  if (info.has_local_saves && defaultExistsOnServer) {
    children.push(
      <div key="custom-fresh-hint" className="romm-panel-muted" style={{ fontSize: "11px", marginTop: "6px" }}>
        {startFreshHintNewSlot()}
      </div>,
    );
  }
  return children;
}

/** Owns the wizard's setup-info lifecycle: the initial load, the reconnect
 *  auto-reload, and the manual Retry — the three paths that share
 *  `offlineHeldRef` and the loading/error state. Kept as a hook so the component
 *  itself only renders; anything that fetches or re-fetches setup info belongs
 *  here, not in the component body. */
function useSaveSetupInfo(romId: number, onComplete: () => void) {
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

  // Manual Retry — user-initiated, so it never auto-confirms (see
  // applyWizardRetrySetupResult); shares offlineHeldRef with the load above.
  const retry = () => {
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
  };

  return { info, loading, confirming, error, setConfirming, setError, retry };
}

export const SlotSetupWizard: FC<SlotSetupWizardProps> = ({ romId, onComplete }) => {
  const { info, loading, confirming, error, setConfirming, setError, retry } = useSaveSetupInfo(romId, onComplete);

  const handleConfirm = createConfirmHandler({ romId, setConfirming, setError, onComplete });

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
        <DialogButton className="romm-wizard-btn" style={btnStyle} onClick={retry}>
          Retry
        </DialogButton>
      </div>
    );
  }

  if (!info) return null;

  const defaultSlot = info.default_slot;

  const leftChildren = buildLocalSavesColumn(info);
  const rightChildren = buildServerSlotsColumn(info, defaultSlot, handleConfirm);

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
