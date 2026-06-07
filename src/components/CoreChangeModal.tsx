import { FC } from "react";
import { ModalRoot, DialogButton, showModal, Navigation } from "@decky/ui";
import { coreSwitchMayBeIgnored } from "../utils/coreSwitch";

// Published docs anchor for the per-game core-switch limitation (System page).
// Heading slug derived from "### Per-game core switching limitation".
const CORE_SWITCH_DOCS_URL =
  "https://danielcopper.github.io/decky-romm-sync/user-guide/bios-management/#per-game-core-switching-limitation";

interface CoreChangeModalProps {
  oldLabel: string;
  newLabel: string;
  // ROM launch filename (basename) RetroDECK awk-matches against gamelist.xml
  // <path>. When it carries regex metacharacters the per-game override may be
  // silently ignored — see coreSwitchMayBeIgnored / issue #210.
  launchFileName?: string | undefined;
  closeModal?: () => void;
  onDone: (proceed: boolean) => void;
}

const CoreChangeModalContent: FC<CoreChangeModalProps> = ({
  oldLabel,
  newLabel,
  launchFileName,
  closeModal,
  onDone,
}) => {
  const handleChoice = (proceed: boolean) => {
    closeModal?.();
    onDone(proceed);
  };

  const mayBeIgnored = launchFileName !== undefined && coreSwitchMayBeIgnored(launchFileName);

  return (
    <ModalRoot
      closeModal={() => {
        closeModal?.();
        onDone(false);
      }}
    >
      <div style={{ padding: "16px", minWidth: "320px" }}>
        <div
          style={{
            fontSize: "16px",
            fontWeight: "bold",
            marginBottom: "4px",
            color: "#fff",
          }}
        >
          Emulator Core Changed
        </div>
        <div
          style={{
            fontSize: "13px",
            color: "rgba(255, 255, 255, 0.6)",
            marginBottom: "16px",
          }}
        >
          {oldLabel} → {newLabel}
        </div>

        <div
          style={{
            padding: "10px",
            background: "rgba(255, 152, 0, 0.15)",
            borderRadius: "4px",
            border: "1px solid rgba(255, 152, 0, 0.3)",
            marginBottom: "12px",
          }}
        >
          <div style={{ fontSize: "12px", color: "#ffb74d", marginBottom: "6px", fontWeight: "bold" }}>
            Save Compatibility Warning
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", lineHeight: "1.4" }}>
            Some emulator cores use incompatible save formats. Continuing may overwrite your existing saves with data
            the previous core can&apos;t read.
          </div>
        </div>

        {mayBeIgnored && (
          <div
            style={{
              padding: "10px",
              background: "rgba(244, 67, 54, 0.15)",
              borderRadius: "4px",
              border: "1px solid rgba(244, 67, 54, 0.3)",
              marginBottom: "16px",
            }}
          >
            <div style={{ fontSize: "12px", color: "#ef9a9a", marginBottom: "6px", fontWeight: "bold" }}>
              Per-Game Core Switch May Be Ignored
            </div>
            <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", lineHeight: "1.4" }}>
              This ROM&rsquo;s filename contains special characters (e.g. parentheses). A known RetroDECK bug can cause
              per-game core overrides to be ignored for such names. If the core does not change in-game, set it
              system-wide on the System page instead.
            </div>
            <DialogButton
              onClick={() => Navigation.NavigateToExternalWeb(CORE_SWITCH_DOCS_URL)}
              style={{ marginTop: "8px", fontSize: "12px", minWidth: "0", width: "auto", padding: "4px 12px" }}
            >
              Learn more
            </DialogButton>
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <DialogButton onClick={() => handleChoice(true)}>Continue</DialogButton>
          <DialogButton onClick={() => handleChoice(false)} style={{ opacity: 0.5 }}>
            Cancel
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

export function showCoreChangeModal(oldLabel: string, newLabel: string, launchFileName?: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    showModal(
      <CoreChangeModalContent
        oldLabel={oldLabel}
        newLabel={newLabel}
        launchFileName={launchFileName}
        onDone={resolve}
      />,
    );
  });
}
