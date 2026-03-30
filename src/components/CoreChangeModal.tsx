import { FC } from "react";
import { ModalRoot, DialogButton, showModal } from "@decky/ui";

interface CoreChangeModalProps {
  oldLabel: string;
  newLabel: string;
  closeModal?: () => void;
  onDone: (proceed: boolean) => void;
}

const CoreChangeModalContent: FC<CoreChangeModalProps> = ({ oldLabel, newLabel, closeModal, onDone }) => {
  const handleChoice = (proceed: boolean) => {
    closeModal?.();
    onDone(proceed);
  };

  return (
    <ModalRoot closeModal={() => { closeModal?.(); onDone(false); }}>
      <div style={{ padding: "16px", minWidth: "320px" }}>
        <div style={{
          fontSize: "16px",
          fontWeight: "bold",
          marginBottom: "4px",
          color: "#fff",
        }}>
          Emulator Core Changed
        </div>
        <div style={{
          fontSize: "13px",
          color: "rgba(255, 255, 255, 0.6)",
          marginBottom: "16px",
        }}>
          {oldLabel} → {newLabel}
        </div>

        <div style={{
          padding: "10px",
          background: "rgba(255, 152, 0, 0.15)",
          borderRadius: "4px",
          border: "1px solid rgba(255, 152, 0, 0.3)",
          marginBottom: "12px",
        }}>
          <div style={{ fontSize: "12px", color: "#ffb74d", marginBottom: "6px", fontWeight: "bold" }}>
            Save Compatibility Warning
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", lineHeight: "1.4" }}>
            Some emulator cores use incompatible save formats. Continuing may
            overwrite your existing saves with data the previous core can't read.
          </div>
        </div>

        <div style={{
          padding: "10px",
          background: "rgba(244, 67, 54, 0.15)",
          borderRadius: "4px",
          border: "1px solid rgba(244, 67, 54, 0.3)",
          marginBottom: "16px",
        }}>
          <div style={{ fontSize: "12px", color: "#ef9a9a", marginBottom: "6px", fontWeight: "bold" }}>
            Per-Game Core Switch Not Working
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", lineHeight: "1.4" }}>
            Due to a RetroDECK bug, per-game core overrides are currently ignored
            for ROMs with special characters in the filename (e.g. parentheses).
            To actually use a different core, switch it system-wide via the QAM panel.
          </div>
        </div>

        {/* TODO: Add wiki link for known incompatibilities once wiki is updated */}
        {/* TODO: Remove per-game warning when RetroDECK fixes awk regex matching (#210) */}

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => handleChoice(true)}>
            Continue
          </DialogButton>
          <DialogButton
            onClick={() => handleChoice(false)}
            style={{ opacity: 0.5 }}
          >
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

export function showCoreChangeModal(
  oldLabel: string,
  newLabel: string,
): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    showModal(
      <CoreChangeModalContent
        oldLabel={oldLabel}
        newLabel={newLabel}
        onDone={resolve}
      />,
    );
  });
}
