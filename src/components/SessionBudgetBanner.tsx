import { FC, ReactNode } from "react";
import { PanelSectionRow, ButtonItem } from "@decky/ui";
import { toaster } from "@decky/api";
import { reloadSteamUi, logError } from "../api/backend";
import { detach } from "../utils/detach";

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
 * Format a signed KB delta as a one-decimal decimal-GB string with an explicit
 * ``+``/``-`` sign, e.g. ``+800000 → "+0.8 GB"``, ``-300000 → "-0.3 GB"``. Zero
 * (and anything rounding to it) reads ``+0.0 GB``. Used for the "last sync" memory
 * delta row (#1383).
 */
export function formatSignedGb(kb: number): string {
  const gb = kb / 1_000_000;
  return `${gb >= 0 ? "+" : "-"}${Math.abs(gb).toFixed(1)} GB`;
}

/**
 * Fire the renderer reload — the "free Steam memory" action. On a REAL reload the
 * JS context is torn down before the promise resolves, so the ``success: true``
 * branch can never actually run and there is no optimistic state to set; Decky
 * reinjects a fresh frontend afterwards. The only response the UI ever observes is
 * a genuine failure (the reload seam couldn't reach Steam, so the UI is still
 * alive) — surface it as a toast. A rejection (transport error) is logged.
 */
function freeSteamMemory(): void {
  detach(
    reloadSteamUi()
      .then((r) => {
        if (!r.success) {
          toaster.toast({ title: "RomM Sync", body: r.message });
        }
      })
      .catch((e) => logError(`Failed to trigger Steam UI reload: ${e}`)),
  );
}

function bannerCard(accent: string, background: string, testId: string, title: string, body: string): ReactNode {
  return (
    <PanelSectionRow>
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
    </PanelSectionRow>
  );
}

interface SessionBudgetBannerProps {
  /** ``stats.last_attempt?.status`` — a ``"paused"`` last run shows the blue resume banner. */
  lastAttemptStatus?: string | undefined;
  /** Live renderer RSS in KB from ``get_session_budget_status``; ``null`` when unreadable. */
  rssKb: number | null;
  /**
   * Disables the "Free Steam memory" button. The banner only renders idle, but the
   * button reloads Steam's renderer, so callers pass ``true`` while anything is
   * mid-flight; the backend also refuses (``sync_active``) as the real guard.
   */
  reloadDisabled?: boolean | undefined;
}

/**
 * Persistent QAM banner for the session-budget UX (#1383). Renders a BLUE/info
 * banner while the last run is ``paused`` (restart Steam, then Resume Sync), or a
 * YELLOW/warning banner when the live renderer heap is high after a completed
 * run. A paused run takes precedence (it is high-heap anyway). Returns nothing
 * when neither applies. When ``rssKb`` is ``null`` (measurement unavailable) the
 * live number is dropped but the guidance text stays. Both banners offer a
 * **Free Steam memory** button that reloads Steam's renderer to reset its heap
 * without a full client restart.
 */
export const SessionBudgetBanner: FC<SessionBudgetBannerProps> = ({ lastAttemptStatus, rssKb, reloadDisabled }) => {
  const paused = lastAttemptStatus === "paused";
  const highHeap = rssKb != null && rssKb > HIGH_HEAP_KB;
  if (!paused && !highHeap) return null;

  const card = paused
    ? bannerCard(
        "#3d9df6",
        "rgba(61, 157, 246, 0.15)",
        "budget-paused-banner",
        "Sync paused",
        `Restart Steam when convenient, then Resume Sync.${
          rssKb != null
            ? ` Steam memory: ${formatGb(rssKb)} (pauses when a chunk would cross ~2.3 GB; Steam crashes near ~2.4 GB, measured on Steam Deck).`
            : ""
        }`,
      )
    : bannerCard(
        "#d4a72c",
        "rgba(212, 167, 44, 0.15)",
        "budget-high-heap-banner",
        "Steam memory is high",
        `Steam memory is high: ${formatGb(rssKb!)} of ~2.4 GB — restart Steam before further large syncs.`,
      );

  return (
    <>
      {card}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={freeSteamMemory}
          disabled={reloadDisabled ?? false}
          description="Reloads Steam's interface (up to a minute) to free memory without a full Steam restart. Running games keep running."
        >
          Free Steam memory
        </ButtonItem>
      </PanelSectionRow>
    </>
  );
};
