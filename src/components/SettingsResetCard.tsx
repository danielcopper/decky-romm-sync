import { FC, createElement } from "react";
import { WarningCard } from "./WarningCard";
import { SETTINGS_RESET_TITLE, settingsResetCardMessage } from "./SettingsResetBanner";

interface SettingsResetCardProps {
  backedUpTo: string | null;
  /** Compact mode for narrow contexts (QAM panel). */
  compact?: boolean;
}

/**
 * Informational warning card shown on the game detail page while a
 * corrupt-settings reset is pending. No dismiss control — the ack lives in the
 * QAM banner; this card clears reactively when the shared store flips to
 * not-pending after the QAM ack.
 */
export const SettingsResetCard: FC<SettingsResetCardProps> = ({ backedUpTo, compact = false }) =>
  createElement(WarningCard, {
    title: SETTINGS_RESET_TITLE,
    message: settingsResetCardMessage(backedUpTo),
    compact,
  });
