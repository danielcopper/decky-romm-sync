import { FC, createElement, useEffect, useState } from "react";
import { FaExclamationTriangle } from "react-icons/fa";
import { getVersionError, onVersionErrorChange } from "../utils/connectionState";

/**
 * Subscribe to version error state changes.
 * Returns the current version error (or null).
 */
export function useVersionError(): string | null {
  const [err, setErr] = useState<string | null>(getVersionError());
  useEffect(() => onVersionErrorChange(setErr), []);
  return err;
}

interface VersionErrorCardProps {
  message: string;
  /** Compact mode for narrow contexts (QAM panel). */
  compact?: boolean;
}

/** Polished error card shown when server version is below plugin minimum. */
export const VersionErrorCard: FC<VersionErrorCardProps> = ({ message, compact = false }) => {
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
      "RomM Server Update Required",
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
      message,
    ),
  );
};
