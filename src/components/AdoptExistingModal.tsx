/**
 * The dialog a download opens instead of writing over content the plugin did not
 * put there (#260, ADR-0028). It states both sides of the collision, offers the
 * content check on a button — never as a wait before the dialog appears — and
 * has three exits: adopt what is there, replace it, or do nothing.
 *
 * Replacing is the only destructive one, so it takes a second confirmation that
 * names the deletion. That confirmation is a step *inside* this modal rather
 * than a nested one: the comparison the user is deciding from stays on screen
 * behind it.
 */

import { FC, useEffect, useState } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import { addEventListener, removeEventListener } from "@decky/api";
import { debugLog, verifyExistingContent } from "../api/backend";
import { formatBytes } from "../utils/formatters";
import { detach } from "../utils/detach";
import type { TargetOccupiedResult, VerifyContentResult, VerifyProgressEvent } from "../types";

export type AdoptChoice = "adopt" | "replace" | "cancel";

interface AdoptExistingModalProps {
  romId: number;
  occupied: TargetOccupiedResult;
  closeModal?: () => void;
  onChoice: (choice: AdoptChoice) => void;
}

const LABEL_STYLE = { fontSize: "12px", color: "rgba(255,255,255,0.55)" };
const VALUE_STYLE = { fontSize: "13px", color: "#fff" };

/** "2026-08-06 14:31" from POSIX epoch seconds; the empty string for a zero stamp. */
function formatTimestamp(epochSeconds: number): string {
  if (!epochSeconds) return "";
  return new Date(epochSeconds * 1000).toLocaleString();
}

/** One sentence on how the two sizes relate — never two bare numbers to subtract. */
function sizeVerdict(occupied: TargetOccupiedResult): string {
  if (occupied.sizes_match === null) return "The server did not state a size, so the two cannot be compared.";
  if (occupied.sizes_match) return "Both are the same size.";
  const delta = occupied.existing.size_bytes - occupied.incoming.size_bytes;
  return delta > 0
    ? `What is here is ${formatBytes(delta)} larger than what the server would send.`
    : `What is here is ${formatBytes(-delta)} smaller than what the server would send.`;
}

const VERIFY_COLORS: Record<VerifyContentResult["status"], string> = {
  match: "#6dd36d",
  mismatch: "#ff8a80",
  unverifiable: "rgba(255,255,255,0.7)",
  missing: "#ff8a80",
  error: "#ff8a80",
};

export const AdoptExistingModal: FC<AdoptExistingModalProps> = ({ romId, occupied, closeModal, onChoice }) => {
  const [verifying, setVerifying] = useState(false);
  const [verifyProgress, setVerifyProgress] = useState<number | null>(null);
  const [verdict, setVerdict] = useState<VerifyContentResult | null>(null);
  const [confirmingReplace, setConfirmingReplace] = useState(false);

  useEffect(() => {
    const listener = addEventListener<[VerifyProgressEvent]>("verify_progress", (payload) => {
      if (payload.rom_id !== romId || !payload.bytes_total) return;
      setVerifyProgress(payload.bytes_done / payload.bytes_total);
    });
    return () => removeEventListener("verify_progress", listener);
  }, [romId]);

  const handleVerify = async () => {
    if (verifying) return;
    setVerifying(true);
    setVerdict(null);
    setVerifyProgress(0);
    try {
      setVerdict(await verifyExistingContent(romId));
    } catch (e) {
      detach(debugLog(`AdoptExistingModal: verify failed: ${e}`));
      setVerdict({ status: "error", message: "Couldn't reach the server to check these files", differences: [] });
    } finally {
      setVerifying(false);
      setVerifyProgress(null);
    }
  };

  const choose = (choice: AdoptChoice) => {
    closeModal?.();
    onChoice(choice);
  };

  const kind = occupied.existing.is_dir ? "folder" : "file";
  const modified = formatTimestamp(occupied.existing.modified_at);

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          This Game Is Already on Your Device
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          A {kind} is already where this game would be downloaded. Tender did not put it there, so it will not be
          touched until you decide.
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px", marginBottom: "8px" }}>
          <div>
            <div style={LABEL_STYLE}>On this device</div>
            <div style={VALUE_STYLE}>{occupied.existing.name}</div>
            <div style={VALUE_STYLE}>{formatBytes(occupied.existing.size_bytes)}</div>
            {modified && <div style={LABEL_STYLE}>Last changed {modified}</div>}
          </div>
          <div>
            <div style={LABEL_STYLE}>On the server</div>
            <div style={VALUE_STYLE}>{occupied.incoming.name}</div>
            <div style={VALUE_STYLE}>
              {occupied.incoming.size_bytes ? formatBytes(occupied.incoming.size_bytes) : "Size unknown"}
            </div>
          </div>
        </div>
        <div style={{ fontSize: "13px", color: "#fff", marginBottom: "12px" }}>{sizeVerdict(occupied)}</div>

        {verifying && (
          <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
            {verifyProgress === null || verifyProgress === 0
              ? "Checking the files…"
              : `Checking the files… ${Math.round(verifyProgress * 100)}%`}
          </div>
        )}
        {!verifying && verdict && (
          <div style={{ marginBottom: "12px" }}>
            <div style={{ fontSize: "13px", color: VERIFY_COLORS[verdict.status] }}>{verdict.message}</div>
            {verdict.differences.map((d) => (
              <div key={d.name} style={{ ...LABEL_STYLE, marginTop: "4px" }}>
                {d.name}: {d.detail}
              </div>
            ))}
          </div>
        )}

        {confirmingReplace ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <div style={{ fontSize: "13px", color: "#ff8a80" }}>
              Downloading deletes the {kind} that is here now — {occupied.existing.name}, {""}
              {formatBytes(occupied.existing.size_bytes)}. If it is your own dump, patch or romhack, it is gone.
              Continue?
            </div>
            <DialogButton onClick={() => choose("replace")}>Delete and Download</DialogButton>
            <DialogButton onClick={() => setConfirmingReplace(false)} style={{ opacity: 0.5 }}>
              Go Back
            </DialogButton>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <DialogButton onClick={() => choose("adopt")} disabled={!occupied.adoptable}>
              {occupied.adoptable ? "Use These Files" : `Can't use this ${kind} for this game`}
            </DialogButton>
            <DialogButton
              onClick={() => {
                detach(handleVerify());
              }}
              disabled={verifying}
            >
              Check Against Server
            </DialogButton>
            <DialogButton onClick={() => setConfirmingReplace(true)}>Download Instead</DialogButton>
            <DialogButton onClick={() => choose("cancel")} style={{ opacity: 0.5 }}>
              Cancel
            </DialogButton>
          </div>
        )}
      </div>
    </ModalRoot>
  );
};

/**
 * Show the dialog and resolve with the exit the user took. Dismissing the modal
 * without pressing anything (outside click, back button) never resolves, so the
 * caller keeps its "nothing happened" state — the same shape as
 * `showCoreChangeModal`.
 */
export function showAdoptExistingModal(romId: number, occupied: TargetOccupiedResult): Promise<AdoptChoice> {
  return new Promise<AdoptChoice>((resolve) => {
    showModal(<AdoptExistingModal romId={romId} occupied={occupied} onChoice={resolve} />);
  });
}
