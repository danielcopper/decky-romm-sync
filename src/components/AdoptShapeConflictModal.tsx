/**
 * The dialog a download opens when something in the platform folder carries this
 * game's name in a form nothing can take over (#260, ADR-0028) — a folder where
 * the server sends a single file, or a file where it sends a folder.
 *
 * There is nothing to adopt here, so this is not a choice between copies. It is
 * the question the plugin would otherwise answer on its own: the download can
 * still run, and what it produces is a **second copy** of the game beside the
 * first. Saying that out loud is the whole point — the button that led here may
 * well have read *Use Existing Files*, and starting a multi-gigabyte transfer
 * with no dialog after that would be the worst of both.
 *
 * Nothing on disk is moved, renamed or removed by either exit.
 */

import { FC } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import type { ShapeConflictResult } from "../types";

export type ShapeConflictChoice = "download" | "cancel";

interface AdoptShapeConflictModalProps {
  conflict: ShapeConflictResult;
  closeModal?: () => void;
  onChoice: (choice: ShapeConflictChoice) => void;
}

const LABEL_STYLE = { fontSize: "12px", color: "rgba(255,255,255,0.55)" };

export const AdoptShapeConflictModal: FC<AdoptShapeConflictModalProps> = ({ conflict, closeModal, onChoice }) => {
  const choose = (choice: ShapeConflictChoice) => {
    closeModal?.();
    onChoice(choice);
  };

  // The server's shape decides both halves of the sentence: everything listed is
  // the other one.
  const servedWord = conflict.served_is_dir ? "a folder of several files" : "a single file";
  const foundWord = conflict.served_is_dir ? "a single file" : "a folder";

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          Something With This Name Is Already Here
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          Your server sends this game as {servedWord}, but what is in this folder is {foundWord}. Tender cannot use it
          as this game, so downloading leaves you with two copies — the one below, and the one it fetches.
        </div>

        <div style={{ marginBottom: "12px" }}>
          {conflict.existing.map((entry) => (
            <div key={entry.path} style={{ fontSize: "13px", color: "#fff", marginBottom: "2px" }}>
              {entry.name} <span style={LABEL_STYLE}>({entry.is_dir ? "folder" : "file"})</span>
            </div>
          ))}
        </div>

        {conflict.truncated && (
          <div style={{ ...LABEL_STYLE, marginBottom: "12px" }}>
            Only the first {conflict.existing.length} are shown — there are more in this folder.
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => choose("download")}>Download {conflict.incoming.name} Anyway</DialogButton>
          <div style={LABEL_STYLE}>
            Nothing above is renamed, moved or deleted — the download lands beside it under your server&apos;s name.
          </div>
          <DialogButton onClick={() => choose("cancel")} style={{ opacity: 0.5 }}>
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

/**
 * Show the dialog and resolve with the exit the user took. Dismissing it without
 * pressing anything never resolves, so the caller keeps its "nothing happened"
 * state — the same shape as its two sibling dialogs.
 */
export function showAdoptShapeConflictModal(conflict: ShapeConflictResult): Promise<ShapeConflictChoice> {
  return new Promise<ShapeConflictChoice>((resolve) => {
    showModal(<AdoptShapeConflictModal conflict={conflict} onChoice={resolve} />);
  });
}
