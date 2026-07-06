import { FC } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";

interface PreferredRegionModalProps {
  oldLabel: string;
  newLabel: string;
  closeModal?: () => void;
  onDone: (proceed: boolean) => void;
}

/**
 * Explains what changing the Preferred-region setting does BEFORE it is saved
 * (ADR-0021 §3). The key semantics the copy must make unmistakable: the setting
 * persists immediately, but it only affects shortcuts minted from the NEXT sync
 * onward (new games / groups without a binding). Already-synced games keep their
 * bound version and shortcut name — the plugin never implicitly switches
 * versions or renames a shortcut. No resync is forced; this modal only explains.
 */
const PreferredRegionModalContent: FC<PreferredRegionModalProps> = ({ oldLabel, newLabel, closeModal, onDone }) => {
  const handleChoice = (proceed: boolean) => {
    closeModal?.();
    onDone(proceed);
  };

  return (
    <ModalRoot
      closeModal={() => {
        closeModal?.();
        onDone(false);
      }}
    >
      <div style={{ padding: "16px", minWidth: "320px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", marginBottom: "4px", color: "#fff" }}>
          Change Preferred Region
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255, 255, 255, 0.6)", marginBottom: "16px" }}>
          {oldLabel} → {newLabel}
        </div>

        <div
          style={{
            padding: "10px",
            background: "rgba(26, 159, 255, 0.12)",
            borderRadius: "4px",
            border: "1px solid rgba(26, 159, 255, 0.3)",
            marginBottom: "12px",
          }}
        >
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.8)", lineHeight: "1.4" }}>
            This applies to games synced <b>from now on</b>. When a game has several regional versions, the plugin will
            prefer this region for the version it binds and the name it gives the new Steam shortcut.
            <br />
            <br />
            Games you have <b>already synced keep their current version and shortcut name</b> — changing this never
            switches a game&apos;s version or renames an existing shortcut. Run a sync to apply it to new games.
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => handleChoice(true)}>Save</DialogButton>
          <DialogButton onClick={() => handleChoice(false)} style={{ opacity: 0.5 }}>
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

/** Show the modal; resolves true if the user confirms, false on cancel / dismiss. */
export function showPreferredRegionModal(oldLabel: string, newLabel: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    showModal(<PreferredRegionModalContent oldLabel={oldLabel} newLabel={newLabel} onDone={resolve} />);
  });
}
