/**
 * The dialog a download opens when something carrying this game's name cannot be
 * read at all (#260, ADR-0028) — a link pointing nowhere, a mount that went
 * away, a race with a writer.
 *
 * It says only what is known. The plugin cannot describe the content, so it can
 * neither offer it nor call it wrong; what it can say is that something with
 * this name is here and cannot be used as this game.
 *
 * **Removal is offered only for a link the backend proved points nowhere** — one
 * entry, one link, no data behind it, so unlinking destroys nothing. For
 * anything else unreadable the exits are download-anyway and cancel, because an
 * entry that merely failed to be read may be the only copy of something.
 */

import { FC } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import type { UnreadableEntryResult } from "../types";

export type UnreadableChoice = { kind: "download" } | { kind: "remove"; path: string } | { kind: "cancel" };

interface AdoptUnreadableModalProps {
  unreadable: UnreadableEntryResult;
  closeModal?: () => void;
  onChoice: (choice: UnreadableChoice) => void;
}

const LABEL_STYLE = { fontSize: "12px", color: "rgba(255,255,255,0.55)" };

/**
 * The one entry removal may be offered for, or `null`.
 *
 * Exactly one, deliberately: the wire carries a single path, and "remove them
 * all" would be a promise this dialog cannot keep for a list it may have capped.
 */
function removableEntry(unreadable: UnreadableEntryResult) {
  const removable = unreadable.existing.filter((entry) => entry.removable);
  return removable.length === 1 && unreadable.existing.length === 1 && !unreadable.truncated ? removable[0] : null;
}

export const AdoptUnreadableModal: FC<AdoptUnreadableModalProps> = ({ unreadable, closeModal, onChoice }) => {
  const choose = (choice: UnreadableChoice) => {
    closeModal?.();
    onChoice(choice);
  };
  const removable = removableEntry(unreadable);

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          Something Here Cannot Be Read
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          An entry in this folder carries this game&apos;s name, but Tender cannot read it — so it cannot tell whether
          it is your copy of the game, and cannot use it as this game. Downloading leaves you with the entry below and
          the copy it fetches.
        </div>

        <div style={{ marginBottom: "12px" }}>
          {unreadable.existing.map((entry) => (
            <div key={entry.path} style={{ fontSize: "13px", color: "#fff", marginBottom: "2px" }}>
              {entry.name}{" "}
              <span style={LABEL_STYLE}>{entry.removable ? "(a link pointing nowhere)" : "(could not be read)"}</span>
            </div>
          ))}
        </div>

        {unreadable.truncated && (
          <div style={{ ...LABEL_STYLE, marginBottom: "12px" }}>
            Only the first {unreadable.existing.length} are shown — there are more in this folder.
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          {removable && (
            <>
              <DialogButton onClick={() => choose({ kind: "remove", path: removable.path })}>
                Remove the Broken Link and Download
              </DialogButton>
              <div style={LABEL_STYLE}>
                The link points at something that is not there, so removing it deletes no game files.
              </div>
            </>
          )}
          <DialogButton onClick={() => choose({ kind: "download" })}>
            Download {unreadable.incoming.name} Anyway
          </DialogButton>
          <div style={LABEL_STYLE}>
            Nothing above is renamed, moved or deleted — the download lands beside it under your server&apos;s name.
          </div>
          <DialogButton onClick={() => choose({ kind: "cancel" })} style={{ opacity: 0.5 }}>
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
 * state — the same shape as its sibling dialogs.
 */
export function showAdoptUnreadableModal(unreadable: UnreadableEntryResult): Promise<UnreadableChoice> {
  return new Promise<UnreadableChoice>((resolve) => {
    showModal(<AdoptUnreadableModal unreadable={unreadable} onChoice={resolve} />);
  });
}
