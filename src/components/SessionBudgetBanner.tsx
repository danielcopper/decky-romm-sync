import { FC, ReactNode } from "react";
import { PanelSectionRow, ButtonItem, Focusable } from "@decky/ui";
import { showToast } from "../utils/toast";
import { isAnyAppRunning } from "../utils/runningApps";

/**
 * Live renderer RSS (KB) above which a completed run recommends a Steam restart.
 * Matches the backend ``domain.session_budget.POST_RUN_ADVISORY_KB`` (#1383).
 */
export const HIGH_HEAP_KB = 1_800_000;

/**
 * Format a KB reading as a one-decimal, decimal-GB string (1 GB = 1e6 KB), e.g.
 * ``2252712 → "2.3 GB"``. Standard rounding.
 */
export function formatGb(kb: number): string {
  return `${(kb / 1_000_000).toFixed(1)} GB`;
}

/**
 * Format a signed KB delta as a one-decimal, unit-less decimal-GB string with an
 * explicit ``+``/``-`` sign, e.g. ``+800000 → "+0.8"``, ``-300000 → "-0.3"``. Zero
 * (and anything rounding to it) reads ``+0.0``. The GB unit is dropped because the
 * only caller renders it inline right after the unit-carrying live reading
 * ("0.6 GB · last run +0.7"), so repeating "GB" would read redundantly (#1383).
 */
export function formatSignedGb(kb: number): string {
  const gb = kb / 1_000_000;
  return `${gb >= 0 ? "+" : "-"}${Math.abs(gb).toFixed(1)}`;
}

/**
 * Traffic-light colour for a live memory reading, decided entirely by the
 * backend-supplied thresholds (no frontend magic numbers, #1383): RED at/above the
 * pause ceiling (every further chunk would pause), YELLOW strictly above the
 * advisory floor (high heap — the same strict trigger as the yellow banner and the
 * backend advisory), else GREEN. The hexes match the existing status palette.
 */
export function memoryLevelColor(rssKb: number, warnKb: number, ceilingKb: number): string {
  if (rssKb >= ceilingKb) return "#d4343c";
  if (rssKb > warnKb) return "#d4a72c";
  return "#59bf40";
}

/**
 * Restart the Steam client — the deterministic "free memory" action. A full client
 * restart resets the renderer's per-session heap budget.
 * Fire-and-forget frontend-side — ``StartRestart`` tears the client down
 * and back up. Hard-guarded on a running game so a click can NEVER kill one
 * mid-session; the button is also disabled while a game runs, but this guard covers
 * the race where a game started between render and click.
 */
function restartSteam(): void {
  if (isAnyAppRunning()) {
    showToast("Close your running game before restarting Steam.");
    return;
  }
  SteamClient.User.StartRestart(false);
}

function bannerCard(accent: string, background: string, testId: string, title: string, body: string): ReactNode {
  return (
    <PanelSectionRow>
      {/* Focusable so Steam's gamepad focus engine can reach and scroll the banner
          into view — this component is QAM-only. The card div keeps the testId +
          styling; the wrapper adds only the focus highlight. */}
      <Focusable>
        <div
          data-testid={testId}
          style={{
            padding: "8px 12px",
            backgroundColor: background,
            borderLeft: `3px solid ${accent}`,
            borderRadius: "4px",
          }}
        >
          <div style={{ fontSize: "13px", fontWeight: "bold", color: accent, marginBottom: "6px" }}>{title}</div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.85)", lineHeight: 1.5 }}>{body}</div>
        </div>
      </Focusable>
    </PanelSectionRow>
  );
}

interface SessionBudgetBannerProps {
  /** ``stats.last_attempt?.status`` — a ``"paused"`` last run shows the blue resume banner. */
  lastAttemptStatus?: string | undefined;
  /** Live renderer RSS in KB from ``get_session_budget_status``; ``null`` when unreadable. */
  rssKb: number | null;
  /**
   * ``resume_ready`` from ``get_session_budget_status`` — ``true`` once the live
   * reading is low enough that resuming a paused run would proceed (e.g. after a
   * Steam restart). Flips the paused banner to "memory is free, press Resume Sync"
   * and hides the restart button. ``false``/``null`` keeps the restart guidance.
   */
  resumeReady?: boolean | null | undefined;
  /**
   * Disables the "Restart Steam now" button for reasons the caller knows about
   * (mid-flight / not connected). The banner ALSO disables it while a game is
   * running — checked here via ``isAnyAppRunning`` — so a restart can never close a
   * running game.
   */
  restartDisabled?: boolean | undefined;
  /**
   * ``run_done_items`` from ``get_session_budget_status`` — how many of the paused
   * run's games are already done. ``null``/absent when the backend doesn't know (a
   * plugin reload wipes the in-memory counters), which drops the progress sentence.
   */
  runDoneItems?: number | null | undefined;
  /** ``run_total_items`` — the denominator of {@link runDoneItems}; the sentence needs both. */
  runTotalItems?: number | null | undefined;
}

/**
 * Persistent QAM banner for the session-budget UX (#1383). Renders a BLUE/info
 * banner while the last run is ``paused`` (restart Steam, then Resume Sync), or a
 * YELLOW/warning banner when the live renderer heap is high after a completed
 * run. A paused run takes precedence (it is high-heap anyway). Returns nothing
 * when neither applies. When ``rssKb`` is ``null`` (measurement unavailable) the
 * live number is dropped but the guidance text stays. Both banners offer a
 * **Restart Steam now** button — a deterministic full client restart that resets
 * the renderer's per-session heap budget — disabled while a game is running.
 */
export const SessionBudgetBanner: FC<SessionBudgetBannerProps> = ({
  lastAttemptStatus,
  rssKb,
  resumeReady,
  restartDisabled,
  runDoneItems,
  runTotalItems,
}) => {
  const paused = lastAttemptStatus === "paused";
  const highHeap = rssKb != null && rssKb > HIGH_HEAP_KB;
  if (!paused && !highHeap) return null;

  // Once the live reading says a resume would proceed (e.g. after a Steam restart),
  // the paused banner announces memory is free and the restart button is pointless.
  const memoryFreedForResume = paused && resumeReady === true;

  const liveReadingSuffix = rssKb != null ? ` (${formatGb(rssKb)})` : "";
  // How far the paused run got. The counts come from the backend — which keeps
  // running across the Steam restart the banner asks for — but a plugin/backend
  // reload wipes them (in-memory, by design). Then, and whenever the total is
  // unknown or zero, the sentence is dropped entirely rather than rendered with
  // placeholders or zeros.
  const progressSentence =
    runDoneItems != null && runTotalItems != null && runTotalItems > 0
      ? ` ${runDoneItems} of ${runTotalItems} games done.`
      : "";
  const pausedBody = memoryFreedForResume
    ? `Steam memory is free again${liveReadingSuffix}.${progressSentence} Press Resume Sync to continue.`
    : `Steam memory is full${liveReadingSuffix}.${progressSentence} Restart Steam, then Resume Sync.`;

  const card = paused
    ? bannerCard("#3d9df6", "rgba(61, 157, 246, 0.15)", "budget-paused-banner", "Sync paused", pausedBody)
    : bannerCard(
        "#d4a72c",
        "rgba(212, 167, 44, 0.15)",
        "budget-high-heap-banner",
        "Steam memory is high",
        `Steam memory is high: ${formatGb(rssKb!)} of 2.4 GB — restart Steam before further large syncs.`,
      );

  // A restart would close a running game, so disable (and hard-guard on click) when
  // one is detected.
  const gameRunning = isAnyAppRunning();
  return (
    <>
      {card}
      {!memoryFreedForResume && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={restartSteam}
            disabled={(restartDisabled ?? false) || gameRunning}
            // Description ONLY for the disabled-by-running-game case, where it explains
            // why the button can't be pressed. The banner body already says what the
            // restart is for, so a description on the enabled button is pure noise.
            description={gameRunning ? "Close your running game first — restarting Steam would close it." : undefined}
          >
            Restart Steam now
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
};
