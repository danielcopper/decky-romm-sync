/**
 * The dialog a download opens when something in the platform folder carries this
 * game's name but cannot become this game's install (#260, ADR-0028).
 *
 * Two things reach it and they are one dialog, because the user's choice is the
 * same for both: an entry of the **other shape** — a folder where the server
 * sends one file, or a file where it sends a folder — and a **symlink**, which
 * is never adoptable whatever it points at, because an install row has to be
 * removable and the uninstall path refuses a link.
 *
 * There is nothing to take over, so this is not a choice between copies. It is
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
import { ENTRY_KIND_LABEL } from "../utils/formatters";
import type { UnusableNamesakeResult } from "../types";

export type UnusableChoice = "download" | "cancel";

interface AdoptUnusableModalProps {
  unusable: UnusableNamesakeResult;
  closeModal?: () => void;
  onChoice: (choice: UnusableChoice) => void;
}

const LABEL_STYLE = { fontSize: "12px", color: "rgba(255,255,255,0.55)" };

export const AdoptUnusableModal: FC<AdoptUnusableModalProps> = ({ unusable, closeModal, onChoice }) => {
  const choose = (choice: UnusableChoice) => {
    closeModal?.();
    onChoice(choice);
  };

  const servedWord = unusable.served_is_dir ? "a folder of several files" : "a single file";

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          Something With This Name Is Already Here
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          Your server sends this game as {servedWord}, and what is in this folder is not something Tender can use as
          this game. Downloading leaves you with two copies — the one below, and the one it fetches.
        </div>

        <div style={{ marginBottom: "12px" }}>
          {unusable.existing.map((entry) => (
            <div key={entry.path} style={{ fontSize: "13px", color: "#fff", marginBottom: "2px" }}>
              {entry.name} <span style={LABEL_STYLE}>({ENTRY_KIND_LABEL[entry.kind]})</span>
            </div>
          ))}
        </div>

        {unusable.truncated && (
          <div style={{ ...LABEL_STYLE, marginBottom: "12px" }}>
            Only the first {unusable.existing.length} are shown — there are more in this folder.
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => choose("download")}>Download {unusable.incoming.name} Anyway</DialogButton>
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
 * Show the dialog and resolve with the user's answer. Dismissing it without
 * pressing anything never resolves, so the caller keeps its "nothing happened"
 * state — the same shape as its sibling dialogs.
 */
export function showAdoptUnusableModal(unusable: UnusableNamesakeResult): Promise<UnusableChoice> {
  return new Promise<UnusableChoice>((resolve) => {
    showModal(<AdoptUnusableModal unusable={unusable} onChoice={resolve} />);
  });
}
