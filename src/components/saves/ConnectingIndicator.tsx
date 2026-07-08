/**
 * Spinner + live retry-progress line for a server-touching load in the saves
 * surfaces (#1345). Replaces the bare italic "Loading…" text so a load that is
 * paying the backend retry+backoff ladder reads as "working" — and, once the
 * `server_retry_progress` store has a value, shows which attempt is in flight
 * ("Connecting to RomM… (attempt 2/3)") instead of a frozen spinner.
 *
 * The throbber reuses the `romm-throbber` class injected by `styleInjector`
 * into the game-detail popup window, so it spins wherever the saves UI renders.
 */

import { createElement, FC } from "react";
import { useServerRetryProgress } from "../../utils/connectionState";
import { MUTED_COLOR } from "./helpers";

interface ConnectingIndicatorProps {
  /** Leading label; the retry "(attempt N/M)" suffix is appended when known. */
  label?: string;
}

export const ConnectingIndicator: FC<ConnectingIndicatorProps> = ({ label = "Connecting to RomM" }) => {
  const progress = useServerRetryProgress();
  const text = progress ? `${label}… (attempt ${progress.attempt}/${progress.maxAttempts})` : `${label}…`;
  return createElement(
    "div",
    { style: { display: "flex", alignItems: "center", gap: "8px", padding: "8px 0" } },
    createElement("span", {
      className: "romm-throbber",
      style: { width: "14px", height: "14px" },
    }),
    createElement("span", { style: { fontSize: "13px", color: MUTED_COLOR } }, text),
  );
};
