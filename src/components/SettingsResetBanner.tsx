import { FC } from "react";
import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { dismissSettingsResetNotice, logError } from "../api/backend";
import { setSettingsResetState } from "../utils/settingsResetStore";
import { detach } from "../utils/detach";

/** Title shared by the QAM banner and the game-detail card. */
export const SETTINGS_RESET_TITLE = "Settings were reset";

/**
 * Body text for the QAM corrupt-settings-reset banner. Names the backup file
 * when known so the user can recover the original bytes; falls back to a
 * generic line when the backup path is unavailable.
 */
export function settingsResetMessage(backedUpTo: string | null): string {
  const backup = backedUpTo
    ? `A backup of your old settings was saved to ${backedUpTo}.`
    : "A backup of your old settings was saved.";
  return `Your settings file was corrupt and has been reset. Re-enter your server URL and sign in again. ${backup}`;
}

/**
 * Body text for the game-detail card. The card is informational only — the
 * Dismiss control lives in the QAM — so the copy points the user there.
 */
export function settingsResetCardMessage(backedUpTo: string | null): string {
  const backup = backedUpTo
    ? `A backup of your old settings was saved to ${backedUpTo}.`
    : "A backup of your old settings was saved.";
  return `Your settings file was corrupt and has been reset. Re-enter your server URL and sign in again. ${backup} Open the RomM Sync menu (QAM) to dismiss this.`;
}

interface SettingsResetBannerProps {
  backedUpTo: string | null;
}

/** QAM PanelSection shown while a corrupt-settings reset is pending. */
export const SettingsResetBanner: FC<SettingsResetBannerProps> = ({ backedUpTo }) => {
  const handleDismiss = () => {
    detach(
      dismissSettingsResetNotice().then(
        // Clear the shared store on success so the banner AND every game-detail
        // card disappear immediately (both subscribe to the same store).
        () => setSettingsResetState({ pending: false, backedUpTo: null }),
        // On failure leave the banner up so the user can retry.
        (e) => logError(`Failed to dismiss settings reset notice: ${e}`),
      ),
    );
  };

  return (
    <PanelSection title={SETTINGS_RESET_TITLE}>
      <PanelSectionRow>
        <div
          style={{
            padding: "8px 12px",
            backgroundColor: "rgba(212, 167, 44, 0.15)",
            borderLeft: "3px solid #d4a72c",
            borderRadius: "4px",
          }}
        >
          <div
            style={{
              fontSize: "13px",
              fontWeight: "bold",
              color: "#d4a72c",
              marginBottom: "6px",
            }}
          >
            {"⚠️"} {SETTINGS_RESET_TITLE}
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.85)", lineHeight: 1.5 }}>
            {settingsResetMessage(backedUpTo)}
          </div>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handleDismiss}>
          Dismiss
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};
