/**
 * SaveSortWarning — the banner the game detail panel shows while a RetroArch
 * save-sorting migration is still pending, i.e. while save file paths written
 * under the old sorting may no longer be where the emulator looks.
 *
 * Uses createElement (no JSX) to match the panel.
 */

import { FC, createElement } from "react";

export const SaveSortWarning: FC = () =>
  createElement(
    "div",
    {
      style: {
        padding: "8px 12px",
        marginBottom: "12px",
        backgroundColor: "rgba(212, 167, 44, 0.15)",
        borderLeft: "3px solid #d4a72c",
        borderRadius: "4px",
      },
    },
    createElement(
      "div",
      {
        style: { fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" },
      },
      "\u26A0\uFE0F RetroArch save sorting changed",
    ),
    createElement(
      "div",
      {
        style: { fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" },
      },
      "Save file paths may be incorrect. Go to Settings to migrate.",
    ),
  );
