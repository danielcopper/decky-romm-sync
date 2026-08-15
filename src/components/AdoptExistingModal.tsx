/**
 * The dialog a download opens instead of writing over content the plugin did not
 * put there, and the one it opens for a file already on the device under a
 * different name (#260, ADR-0028). It states both sides of the comparison,
 * offers the content check on a button — never as a wait before the dialog
 * appears — and has three exits: use what is there, download instead, or do
 * nothing.
 *
 * The two cases differ in one sentence and one consequence. A file at the game's
 * own location is used where it lies; a candidate elsewhere in the folder is
 * **renamed** into place, saves and savestates with it, so an adopted install
 * ends up indistinguishable from a downloaded one. Both are stated up front,
 * because the rename is a change to the user's own filing.
 *
 * Downloading is the only destructive exit, so it takes a second confirmation
 * that names the deletion. That confirmation is a step *inside* this modal
 * rather than a nested one: the comparison the user is deciding from stays on
 * screen behind it.
 */

import { FC, useEffect, useState } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import { addEventListener, removeEventListener } from "@decky/api";
import { debugLog, verifyExistingContent } from "../api/backend";
import { ENTRY_KIND_LABEL, formatBytes } from "../utils/formatters";
import { detach } from "../utils/detach";
import type { AdoptionCandidate, TargetOccupiedResult, VerifyContentResult, VerifyProgressEvent } from "../types";

export type AdoptChoice = "adopt" | "replace" | "cancel";

interface AdoptExistingModalProps {
  romId: number;
  occupied: TargetOccupiedResult;
  /**
   * Set when `occupied` describes a candidate found elsewhere in the platform
   * folder rather than content at the game's own location. It is the path the
   * content check runs against, and its presence is what tells the user the
   * file will be renamed.
   */
  candidatePath?: string | undefined;
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

/**
 * The noun for what is in the way. A `null` kind is something the backend looked
 * at and has no word for — a named pipe, a socket — so this has none either,
 * rather than calling it a file: that guess is what let one be offered as a game.
 */
function nounFor(occupied: TargetOccupiedResult): string {
  const kind = occupied.existing.kind;
  return kind === null ? "thing" : ENTRY_KIND_LABEL[kind];
}

/**
 * Whether what is at the path is the game's own content, and so whether the
 * numbers `stat` returned describe the game at all. Only a file or a directory
 * is: a symlink's size and mtime are the link's own — when it was pointed
 * somewhere, not when the game was last touched — and a kindless entry's belong
 * to something the plugin has no word for.
 *
 * Everything under "On this device" is read as being about this game's copy, so
 * a measurement that is not gets left out rather than qualified. Half that
 * column already says so where the size would be; a bare "Last changed" beside
 * it is the one line still implying otherwise.
 */
function describesTheGame(occupied: TargetOccupiedResult): boolean {
  return occupied.existing.kind === "file" || occupied.existing.kind === "dir";
}

/**
 * How the existing side's size is stated. Only a file or a folder has a byte
 * count that is the game's; a shortcut's `stat` reports the length of the path
 * it stores, which is a real-looking number about nothing the user is deciding
 * on, and a kindless entry's is not the game's either. Those say so instead —
 * printing the number and disclaiming it two lines below still puts it beside
 * the server's real one, to be read as a comparison.
 *
 * A candidate folder is the third case and a different reason: the search stays
 * on the platform folder's top level, because descending into one multi-file
 * game can mean tens of thousands of files, so nothing measured it. "0 B" about
 * something that may be gigabytes is the one thing this must not print.
 */
function existingSize(occupied: TargetOccupiedResult, candidate: boolean): string {
  if (candidate && occupied.existing.kind === "dir") return "Folder — not measured";
  if (occupied.existing.kind === "link") return "Shortcut — no size of its own";
  if (occupied.existing.kind === null) return "No size to show";
  return formatBytes(occupied.existing.size_bytes);
}

/**
 * One sentence on how the two sizes relate — never two bare numbers to subtract,
 * and never our own choice not to measure reported as the server's silence.
 */
function sizeVerdict(occupied: TargetOccupiedResult, candidate: boolean): string {
  if (candidate && occupied.existing.kind === "dir") {
    return "Folders are not measured before you open this, so the two sizes are not compared.";
  }
  if (occupied.existing.kind === "link") {
    return "A shortcut is not the game's bytes, so there is nothing here to compare.";
  }
  if (occupied.existing.kind === null) return "This is not a file or a folder, so there is nothing here to compare.";
  if (occupied.sizes_match === null) return "The server did not state a size, so the two cannot be compared.";
  if (occupied.sizes_match) return "Both are the same size.";
  const delta = occupied.existing.size_bytes - occupied.incoming.size_bytes;
  return delta > 0
    ? `What is here is ${formatBytes(delta)} larger than what the server would send.`
    : `What is here is ${formatBytes(-delta)} smaller than what the server would send.`;
}

/**
 * What the second confirmation promises will be destroyed. Three sentences,
 * because three different things are: a file or folder may be the user's own
 * dump and is gone for good, a shortcut is one line of filesystem bookkeeping
 * whose target survives, and a kindless entry is something the plugin can only
 * say it is removing.
 */
function replaceWarning(occupied: TargetOccupiedResult, candidate: boolean, noun: string): string {
  const name = occupied.existing.name;
  if (occupied.existing.kind === "link") {
    return `Downloading deletes the shortcut that is here now — ${name}. Whatever it points at is left alone. Continue?`;
  }
  if (occupied.existing.kind === null) {
    return `Downloading removes what is here now — ${name}. Tender cannot tell what it is, only that it goes. Continue?`;
  }
  return (
    `Downloading deletes the ${noun} that is here now — ${name}, ${existingSize(occupied, candidate)}. ` +
    "If it is your own dump, patch or romhack, it is gone. Continue?"
  );
}

const VERIFY_COLORS: Record<VerifyContentResult["status"], string> = {
  match: "#6dd36d",
  mismatch: "#ff8a80",
  unverifiable: "rgba(255,255,255,0.7)",
  missing: "#ff8a80",
  error: "#ff8a80",
};

export const AdoptExistingModal: FC<AdoptExistingModalProps> = ({
  romId,
  occupied,
  candidatePath,
  closeModal,
  onChoice,
}) => {
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
      setVerdict(await verifyExistingContent(romId, candidatePath ?? null));
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

  const noun = nounFor(occupied);
  const modified = formatTimestamp(occupied.existing.modified_at);

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          This Game Is Already on Your Device
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          {candidatePath
            ? `This ${noun} carries this game's name. Tender did not put it there, so it will not be touched until ` +
              "you decide."
            : `A ${noun} is already where this game would be downloaded. Tender did not put it there, so it will not ` +
              "be touched until you decide."}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px", marginBottom: "8px" }}>
          <div>
            <div style={LABEL_STYLE}>On this device</div>
            <div style={VALUE_STYLE}>{occupied.existing.name}</div>
            <div style={VALUE_STYLE}>{existingSize(occupied, Boolean(candidatePath))}</div>
            {modified && describesTheGame(occupied) && <div style={LABEL_STYLE}>Last changed {modified}</div>}
          </div>
          <div>
            <div style={LABEL_STYLE}>On the server</div>
            <div style={VALUE_STYLE}>{occupied.incoming.name}</div>
            <div style={VALUE_STYLE}>
              {occupied.incoming.size_bytes ? formatBytes(occupied.incoming.size_bytes) : "Size unknown"}
            </div>
          </div>
        </div>
        <div style={{ fontSize: "13px", color: "#fff", marginBottom: "12px" }}>
          {sizeVerdict(occupied, Boolean(candidatePath))}
        </div>
        {candidatePath && (
          <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
            Using it renames it to {occupied.incoming.name}, and moves any saves and savestates named after it with it,
            so this game works the same as one Tender downloaded.
          </div>
        )}

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
              {replaceWarning(occupied, Boolean(candidatePath), noun)}
            </div>
            <DialogButton onClick={() => choose("replace")}>Delete and Download</DialogButton>
            <DialogButton onClick={() => setConfirmingReplace(false)} style={{ opacity: 0.5 }}>
              Go Back
            </DialogButton>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <DialogButton onClick={() => choose("adopt")} disabled={!occupied.adoptable}>
              {occupied.adoptable ? "Use These Files" : `Can't use this ${noun} for this game`}
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
export function showAdoptExistingModal(
  romId: number,
  occupied: TargetOccupiedResult,
  candidatePath?: string,
): Promise<AdoptChoice> {
  return new Promise<AdoptChoice>((resolve) => {
    showModal(
      <AdoptExistingModal romId={romId} occupied={occupied} candidatePath={candidatePath} onChoice={resolve} />,
    );
  });
}

/**
 * Reshape one candidate into the comparison the dialog renders. `adoptable` is
 * unconditionally true: the search only ever offers an entry whose shape matches
 * what the server serves, so there is no unusable candidate to disable the
 * button for.
 */
export function comparisonForCandidate(
  candidate: AdoptionCandidate,
  incoming: { name: string; size_bytes: number },
): TargetOccupiedResult {
  return {
    success: false,
    reason: "target_occupied",
    message: `'${candidate.name}' is already on this device`,
    existing: {
      name: candidate.name,
      path: candidate.path,
      // The search offers only what an install row may point at, so a candidate
      // is one of the two adoptable kinds and never a link.
      kind: candidate.is_dir ? "dir" : "file",
      size_bytes: candidate.size_bytes,
      modified_at: candidate.modified_at,
    },
    incoming,
    // A directory candidate is never sized by the search — it does not descend —
    // so there are no two numbers to relate. Same `null` as "the server stated no
    // size", but NOT the same sentence: `existingSize` / `sizeVerdict` tell the
    // two apart on the kind, because attributing our own choice not to measure to
    // the server would be a claim about their setup that is simply untrue.
    sizes_match: candidate.is_dir || !incoming.size_bytes ? null : candidate.size_bytes === incoming.size_bytes,
    adoptable: true,
  };
}
