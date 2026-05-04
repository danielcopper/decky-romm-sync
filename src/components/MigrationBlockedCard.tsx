import { FC, createElement } from "react";
import { FaExclamationTriangle } from "react-icons/fa";

interface MigrationBlockedCardProps {
  /** Compact mode for narrow contexts (QAM panel). */
  compact?: boolean;
}

/** Polished warning card shown on the game detail page when a RetroDECK migration is pending. */
export const MigrationBlockedCard: FC<MigrationBlockedCardProps> = ({ compact = false }) => {
  return createElement(
    "div",
    {
      style: {
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: compact ? "24px 16px" : "40px 32px",
        gap: "14px",
        textAlign: "center",
        background: "rgba(14, 20, 27, 0.55)",
        border: "1px solid rgba(255, 170, 0, 0.35)",
        borderRadius: "6px",
        margin: compact ? "8px 4px" : "24px 2.8vw",
      },
    },
    createElement(FaExclamationTriangle, {
      style: { color: "#ffaa00", fontSize: compact ? "28px" : "42px" },
    }),
    createElement(
      "div",
      {
        style: {
          fontSize: compact ? "15px" : "19px",
          fontWeight: 600,
          color: "rgba(255, 255, 255, 0.95)",
        },
      },
      "RetroDECK Migration Required",
    ),
    createElement(
      "div",
      {
        style: {
          fontSize: compact ? "12px" : "14px",
          color: "rgba(255, 255, 255, 0.75)",
          maxWidth: compact ? "100%" : "680px",
          lineHeight: 1.5,
        },
      },
      "Open the plugin QAM to migrate files or dismiss the migration before playing.",
    ),
  );
};
