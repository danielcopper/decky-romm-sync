/**
 * AchievementsTab — the RetroAchievements pane of the RomM game detail panel.
 *
 * Owns the whole lane: the lazy list + progress load, its load-once gate, and
 * the rendered badge rows. Nothing about it reaches the panel, which knows only
 * the `ra_id` that decides whether the tab exists at all.
 *
 * Stays mounted while another tab is showing (rendering nothing) so a loaded
 * list survives a tab switch instead of re-fetching on every visit.
 *
 * Uses createElement (no JSX) to match the panel. CSS classes prefixed with
 * `romm-cheevo-` are injected separately by styleInjector.
 */

import { useState, useEffect, useRef, FC, createElement } from "react";
import { DialogButton } from "@decky/ui";
import { getAchievements, getAchievementProgress, debugLog } from "../api/backend";
import type { Achievement, AchievementProgress, EarnedAchievement } from "../types";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import {
  beginServerLoad,
  reportServerReachable,
  setServerRetryProgress,
  settleServerLoad,
  useRommConnectionState,
} from "../utils/connectionState";
import { ConnectingIndicator } from "./saves/ConnectingIndicator";
import { detach } from "../utils/detach";

interface AchievementsTabProps {
  romId: number;
  /** The ROM's RetroAchievements game id — without one there is nothing to load. */
  raId: number | null;
  isActive: boolean;
}

/** "2025-02-14 15:45:38" -> "2025-02-14 15:45" */
function formatCheevoDate(dateStr: string): string {
  return dateStr.replace(/:\d{2}$/, "");
}

/** Deterministic sparkle positions for a hardcore badge, seeded per achievement
 *  so the same badge always sparkles the same way across re-renders. */
function makeHcSparkles(seed: number) {
  const rng = (i: number) => {
    const x = Math.sin(seed * 9301 + i * 4973) * 49297;
    return x - Math.floor(x);
  };
  return Array.from({ length: 4 }, (_, i) => ({
    top: `${Math.round(rng(i * 3) * 100)}%`,
    left: `${Math.round(rng(i * 3 + 1) * 100)}%`,
    dur: 2.2 + rng(i * 3 + 2) * 1.8, // 2.2–4.0s
    delay: rng(i * 7 + 5) * 2, // 0–2.0s
  }));
}

/** Render one achievement row — badge, title/description/rarity, earned dates,
 *  points. Earned state (and with it the hardcore treatment) comes from
 *  `earnedMap`, keyed by badge_id. */
function renderCheevoRow(a: Achievement, earnedMap: Map<string, EarnedAchievement>) {
  const earnedData = earnedMap.get(a.badge_id);
  const isEarned = !!earnedData;
  const isHardcore = !!earnedData?.date_hardcore;

  const rowClasses = ["romm-cheevo-row", isEarned ? "romm-cheevo-row-earned" : ""].filter(Boolean).join(" ");

  const imgClasses = ["romm-cheevo-badge-img", isHardcore ? "romm-cheevo-badge-img-hc" : ""].filter(Boolean).join(" ");

  // Date column for earned achievements — show both normal and HC dates
  const dateChildren: ReturnType<typeof createElement>[] = [];
  if (earnedData?.date) {
    dateChildren.push(
      createElement("span", { key: "date", className: "romm-cheevo-date" }, formatCheevoDate(earnedData.date)),
    );
  }
  if (isHardcore && earnedData.date_hardcore) {
    dateChildren.push(
      createElement(
        "span",
        {
          key: "hc-row",
          style: { display: "inline-flex", alignItems: "center", gap: "4px" },
        },
        createElement("span", { className: "romm-cheevo-hc-badge" }, "HC"),
        createElement("span", { className: "romm-cheevo-date" }, formatCheevoDate(earnedData.date_hardcore)),
      ),
    );
  }

  // Badge image — wrapped with sparkle container for HC achievements
  const imgEl = createElement("img", {
    className: imgClasses,
    src: isEarned ? a.badge_url : a.badge_url_lock || a.badge_url,
    style: isEarned ? {} : { filter: "grayscale(0.7) opacity(0.6)" },
  });

  const badgeElement = isHardcore
    ? createElement(
        "div",
        { className: "romm-cheevo-img-wrap" },
        imgEl,
        createElement(
          "span",
          { className: "romm-cheevo-img-sparkles" },
          ...makeHcSparkles(a.ra_id).map((sp) =>
            createElement("span", {
              key: `hc-sp-${sp.top}-${sp.left}`,
              className: "romm-cheevo-img-sparkle-dot",
              style: {
                "--romm-sparkle-top": sp.top,
                "--romm-sparkle-left": sp.left,
                "--romm-sparkle-delay": `${sp.delay.toFixed(1)}s`,
                "--romm-sparkle-dur": `${sp.dur.toFixed(1)}s`,
              } satisfies CSSPropertiesWithVars,
            }),
          ),
        ),
      )
    : imgEl;

  return createElement(
    DialogButton,
    {
      key: `cheevo-${a.ra_id}`,
      className: rowClasses,
      noFocusRing: false,
      onFocus: scrollFocusedToCenter,
      style: {
        background: "transparent",
        border: "none",
        padding: 0,
        textAlign: "left" as const,
        cursor: "default",
        display: "flex",
        alignItems: "center",
        gap: "12px",
      },
    },
    badgeElement,
    createElement(
      "div",
      { className: "romm-cheevo-details" },
      createElement("div", { className: "romm-cheevo-title" }, a.title),
      createElement("div", { className: "romm-cheevo-desc" }, a.description),
      a.num_awarded > 0
        ? createElement("div", { className: "romm-cheevo-rarity" }, `${a.num_awarded} players earned this`)
        : null,
    ),
    dateChildren.length > 0 ? createElement("div", { className: "romm-cheevo-dates" }, ...dateChildren) : null,
    createElement(
      "div",
      {
        className: `romm-cheevo-points ${isEarned ? "" : "romm-cheevo-points-locked"}`,
      },
      `${a.points} pts`,
    ),
  );
}

/** The list body: summary bar, progress bar, then the earned and locked rows —
 *  earned first, each group in display order. */
function renderCheevoList(
  achievements: Achievement[],
  progress: AchievementProgress | null,
): ReturnType<typeof createElement> {
  const earned = progress?.earned ?? 0;
  const total = progress?.total ?? achievements.length;

  // Build map from badge_id -> earned data (id in earned_achievements is badge_id)
  const earnedMap = new Map<string, EarnedAchievement>();
  for (const ea of progress?.earned_achievements ?? []) {
    earnedMap.set(ea.id, ea);
  }

  // Sort: earned first, then by display_order
  const sorted = [...achievements].sort((a, b) => {
    const aEarned = earnedMap.has(a.badge_id) ? 0 : 1;
    const bEarned = earnedMap.has(b.badge_id) ? 0 : 1;
    if (aEarned !== bEarned) return aEarned - bEarned;
    return (a.display_order || 0) - (b.display_order || 0);
  });

  const earnedList = sorted.filter((a) => earnedMap.has(a.badge_id));
  const lockedList = sorted.filter((a) => !earnedMap.has(a.badge_id));

  const cheevoChildren: ReturnType<typeof createElement>[] = [];

  // Summary bar
  cheevoChildren.push(
    createElement(
      "div",
      { key: "summary", className: "romm-cheevo-summary" },
      createElement("span", { className: "romm-cheevo-summary-text" }, `${earned} / ${total} Achievements`),
      progress?.earned_hardcore
        ? createElement("span", { className: "romm-cheevo-summary-sub" }, `${progress.earned_hardcore} hardcore`)
        : null,
    ),
  );

  // Progress bar
  const pct = total > 0 ? (earned / total) * 100 : 0;
  cheevoChildren.push(
    createElement(
      "div",
      { key: "progress-bar", className: "romm-cheevo-progress-bar" },
      createElement("div", {
        className: "romm-cheevo-progress-fill",
        style: { width: `${pct}%` },
      }),
    ),
  );

  if (earnedList.length > 0) {
    cheevoChildren.push(
      createElement(
        "div",
        { key: "earned-title", className: "romm-cheevo-section-title" },
        `Earned (${earnedList.length})`,
      ),
    );
    earnedList.forEach((a) => cheevoChildren.push(renderCheevoRow(a, earnedMap)));
  }

  if (lockedList.length > 0) {
    cheevoChildren.push(
      createElement(
        "div",
        { key: "locked-title", className: "romm-cheevo-section-title" },
        `Locked (${lockedList.length})`,
      ),
    );
    lockedList.forEach((a) => cheevoChildren.push(renderCheevoRow(a, earnedMap)));
  }

  // Not wrapped in a section() — that would make the whole list ONE giant
  // focusable element. The individual rows are DialogButtons instead, which is
  // what gives the pane focus-driven scrolling.
  return createElement("div", { className: "romm-cheevo-list" }, ...cheevoChildren);
}

export const AchievementsTab: FC<AchievementsTabProps> = ({ romId, raId, isActive }) => {
  const isOffline = useRommConnectionState() === "offline";
  const [achievements, setAchievements] = useState<Achievement[]>([]);
  const [progress, setProgress] = useState<AchievementProgress | null>(null);
  const [loading, setLoading] = useState(false);
  const loadedRef = useRef(false);

  // Lazy-load on first activation. Mirrors the panel's saves-slot load's offline
  // handling (#1345 F1): a known-offline fast path (no ladder hang), a
  // reachability feed, auto-reload on reconnect, and a mid-flight-teardown guard
  // so a store flip can't wedge the spinner.
  useEffect(() => {
    if (!isActive || !raId) return;
    if (loadedRef.current) return;

    // Known-offline fast path: the server fetch runs the retry ladder, so on a
    // known-unreachable server it would hang "Loading achievements…" for tens of
    // seconds. Skip it — the render shows a short degraded line while any
    // last-known list stays visible. The ref stays false, so a flip back to
    // connected re-runs this effect (isOffline dep) and loads.
    if (isOffline) return;
    loadedRef.current = true;

    const load = beginServerLoad();
    let cancelled = false;
    let settled = false;

    async function loadAchievements() {
      // Drop stale retry progress from a prior load so the ConnectingIndicator
      // starts at a plain "Loading achievements…" (#1345).
      setServerRetryProgress(null);
      setLoading(true);
      try {
        const [listResult, progressResult] = await Promise.all([getAchievements(romId), getAchievementProgress(romId)]);
        if (cancelled) return;
        settled = true;
        // Conservative reachability feed (#1345): report offline only on a
        // genuine unreachable verdict from either call. Treat a resolved
        // non-stale success as a connected signal — this can be cache-served
        // (get_achievements / get_achievement_progress answer from a warm cache
        // without touching the server), so it is not a hard reachability proof,
        // but that is acceptable: the 30s heartbeat is the reachability authority
        // and self-corrects a wrong "connected". A "no_ra_username" config gap and
        // a stale-cache fallback are neither verdict — leave the store untouched.
        const unreachable =
          listResult.reason === "server_unreachable" || progressResult.reason === "server_unreachable";
        if (unreachable) {
          reportServerReachable(false);
          // Mirror the slot lane's failure reset: release the gate so a reconnect
          // (or a later re-activation) retries instead of caching the failure.
          loadedRef.current = false;
        } else if ((listResult.success && !listResult.stale) || (progressResult.success && !progressResult.stale)) {
          reportServerReachable(true);
        }
        // Keep the last-known values on a failed load — never clobber an
        // already-shown list / progress count to empty on a transient blip.
        if (listResult.success) setAchievements(listResult.achievements);
        if (progressResult.success) setProgress(progressResult);
        setLoading(false);
      } catch (e) {
        detach(debugLog(`Failed to load achievements: ${e}`));
        if (!cancelled) {
          settled = true;
          loadedRef.current = false;
          setLoading(false);
        }
      } finally {
        settleServerLoad(load);
      }
    }

    detach(loadAchievements());
    return () => {
      cancelled = true;
      // Torn down mid-flight (e.g. a concurrent call flipped the store offline) —
      // release the gate and drop the spinner so the re-run / reconnect isn't
      // wedged behind a stuck loading flag (#1345 F1, mirrors the slots lane).
      if (!settled) {
        loadedRef.current = false;
        setLoading(false);
      }
    };
  }, [isActive, raId, romId, isOffline]);

  if (!isActive) return null;

  if (loading) {
    // The load pays the backend retry ladder — surface the shared
    // ConnectingIndicator (with live "(attempt N/M)" progress) instead of
    // frozen "Loading…" text (#1345).
    return createElement(ConnectingIndicator, { label: "Loading achievements" });
  }

  if (achievements.length === 0) {
    // No cached list to fall back on. When the server is known-offline this is
    // the degraded state for the fast path (no ladder hang); otherwise it's the
    // genuine "this game has none" case (#1345).
    return createElement(
      "div",
      { className: "romm-panel-muted" },
      isOffline ? "RomM offline — achievements unavailable." : "No achievements found for this game",
    );
  }

  return renderCheevoList(achievements, progress);
};
