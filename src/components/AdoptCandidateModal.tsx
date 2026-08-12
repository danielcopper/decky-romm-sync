/**
 * The short list a download opens when this game is already on the device under
 * a different name (#260, ADR-0028). One row per file, strongest evidence first,
 * each stating what its offer rests on — nothing here has read a byte of
 * content, so the list is a starting point for the comparison dialog and never a
 * verdict.
 *
 * Shown only for two or more candidates: with exactly one there is nothing to
 * choose between, and the caller opens the comparison directly.
 */

import { FC } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import { formatBytes } from "../utils/formatters";
import type { AdoptionCandidate, CandidatesFoundResult } from "../types";

export type CandidateChoice =
  { kind: "candidate"; candidate: AdoptionCandidate } | { kind: "download" } | { kind: "cancel" };

interface AdoptCandidateModalProps {
  found: CandidatesFoundResult;
  closeModal?: () => void;
  onChoice: (choice: CandidateChoice) => void;
}

const LABEL_STYLE = { fontSize: "12px", color: "rgba(255,255,255,0.55)" };

export const AdoptCandidateModal: FC<AdoptCandidateModalProps> = ({ found, closeModal, onChoice }) => {
  const choose = (choice: CandidateChoice) => {
    closeModal?.();
    onChoice(choice);
  };

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          This Game May Already Be on Your Device
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          These files sit in the same folder and carry this game&apos;s name. Tender did not put them there, so nothing
          is touched until you pick one.
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "8px", marginBottom: "12px" }}>
          {found.candidates.map((candidate) => (
            <DialogButton key={candidate.path} onClick={() => choose({ kind: "candidate", candidate })}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
                <div style={{ fontSize: "13px", color: "#fff" }}>{candidate.name}</div>
                <div style={LABEL_STYLE}>
                  {candidate.detail}
                  {candidate.is_dir ? " — folder" : ` — ${formatBytes(candidate.size_bytes)}`}
                </div>
              </div>
            </DialogButton>
          ))}
        </div>

        {found.truncated && (
          <div style={{ ...LABEL_STYLE, marginBottom: "12px" }}>
            Only the {found.candidates.length} strongest matches are shown — there are more in this folder.
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => choose({ kind: "download" })}>
            None of These — Download {found.incoming.name}
          </DialogButton>
          <DialogButton onClick={() => choose({ kind: "cancel" })} style={{ opacity: 0.5 }}>
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

/**
 * Show the list and resolve with the exit the user took. Dismissing the modal
 * without pressing anything never resolves, so the caller keeps its "nothing
 * happened" state — the same shape as `showAdoptExistingModal`.
 */
export function showAdoptCandidateModal(found: CandidatesFoundResult): Promise<CandidateChoice> {
  return new Promise<CandidateChoice>((resolve) => {
    showModal(<AdoptCandidateModal found={found} onChoice={resolve} />);
  });
}
