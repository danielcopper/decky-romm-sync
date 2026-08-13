/**
 * The second dialog both exits of the adopt dialog open when names they need are
 * already taken — the user has played both versions and both left saves behind
 * (#260). Use These Files renames the game and its saves; Download Instead
 * deletes the game and moves only its saves. So the wording names the files, not
 * the game: on the download path the game is not renamed at all.
 *
 * It opens **before a single file has moved**: the backend computes every
 * source → target pair and checks all of them first, so this asks once for the
 * whole set rather than once per collision with half the set already moved.
 *
 * **Neither exit destroys anything, and both say so.** Replace moves the files it
 * displaces into `.romm-backup` — the same funnel every other save the plugin
 * replaces goes through — so this is the surface that has to tell the user, since
 * it is the only one they see while choosing. Keep leaves them alone, but leaves
 * the old-named ones orphaned; implying that move was clean would be the one
 * thing this dialog must not do.
 */

import { FC } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";
import type { CollisionChoice, RenameCollision } from "../types";

interface AdoptCollisionModalProps {
  collisions: RenameCollision[];
  closeModal?: () => void;
  onChoice: (choice: CollisionChoice | "cancel") => void;
}

const LABEL_STYLE = { fontSize: "12px", color: "rgba(255,255,255,0.55)" };

const KIND_LABEL: Record<RenameCollision["kind"], string> = {
  rom: "game file",
  save: "save",
  savestate: "savestate",
};

export const AdoptCollisionModal: FC<AdoptCollisionModalProps> = ({ collisions, closeModal, onChoice }) => {
  const choose = (choice: CollisionChoice | "cancel") => {
    closeModal?.();
    onChoice(choice);
  };

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "420px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "4px" }}>
          Some of These Names Are Taken
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.7)", marginBottom: "12px" }}>
          Moving this game&apos;s files to the name your server uses would land on files that already exist. Nothing has
          been moved yet.
        </div>

        <div style={{ marginBottom: "12px" }}>
          {collisions.map((collision) => (
            <div key={collision.path} style={{ fontSize: "13px", color: "#fff", marginBottom: "2px" }}>
              {collision.name} <span style={LABEL_STYLE}>({KIND_LABEL[collision.kind]})</span>
            </div>
          ))}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => choose("overwrite")}>Replace Them</DialogButton>
          <DialogButton onClick={() => choose("keep")}>Keep Them</DialogButton>
          <div style={LABEL_STYLE}>
            Replace does not delete the files listed above — each is moved into a .romm-backup folder beside it, so you
            can put one back by hand if you pick wrong. Keep leaves them alone and leaves this game&apos;s old-named
            saves where they are — nothing is lost, but nothing will be reading them either.
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
 * Show the dialog and resolve with the user's one answer for the whole set.
 * Dismissing it without pressing anything never resolves, so the caller keeps
 * its "nothing happened" state.
 */
export function showAdoptCollisionModal(collisions: RenameCollision[]): Promise<CollisionChoice | "cancel"> {
  return new Promise<CollisionChoice | "cancel">((resolve) => {
    showModal(<AdoptCollisionModal collisions={collisions} onChoice={resolve} />);
  });
}
