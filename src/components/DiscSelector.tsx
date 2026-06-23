/**
 * DiscSelector — inline disc picker for multi-disc ROMs (#865).
 *
 * Sits immediately to the right of CustomPlayButton in the play-section row.
 * For a multi-disc install it renders a compact, icon-only trigger whose face
 * IS the badge: a stacked-discs glyph (neutral) for the m3u "all discs" default,
 * or a single disc + number (accent) when a specific disc is pinned. Clicking it
 * opens an anchored `showContextMenu` list of discs. Picking a disc rewrites the
 * Steam shortcut's `launch_options` to that disc's file (emulator-agnostic) and
 * persists the choice in the backend DB, so the Play button always launches the
 * currently-selected disc.
 *
 * Single-disc / unknown / not-installed ROMs render nothing (zero footprint).
 * The picker re-fetches on `download_complete` (a newly installed ROM may now
 * be multi-disc) and hides on `romm_rom_uninstalled`.
 */

import { useState, useEffect, useRef, FC, ReactNode } from "react";
import { addEventListener, removeEventListener, toaster } from "@decky/api";
import { Menu, MenuItem, showContextMenu, DialogButton } from "@decky/ui";
import { FaCompactDisc, FaChevronDown } from "react-icons/fa";
import { getCachedGameDetail, getDiscSelection, selectDisc, logError } from "../api/backend";
import type { DiscSelection } from "../api/backend";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { getEventTarget } from "../utils/events";
import { detach } from "../utils/detach";
import type { DownloadCompleteEvent } from "../types";

interface DiscSelectorProps {
  appId: number;
}

/** A disc option's `data` value: a disc filename, or `null` for the m3u default. */
type DiscOptionData = string | null;

// Neutral grey for the m3u default; Steam accent blue when a specific disc is
// pinned — an instant "this isn't the default" read.
const DISC_GREY = "#dcdedf";
const DISC_ACCENT = "#59b6ff";

/** Two CDs stacked top-left → bottom-right: the front (opaque) disc at the
 * top-left, one behind it trailing down-right and faded — the m3u "all discs"
 * face. The back disc renders first so the front one is on top. */
const DiscStack: FC<{ size: number; color: string }> = ({ size, color }) => {
  const step = Math.round(size * 0.3);
  return (
    <span style={{ position: "relative", display: "inline-block", width: size + step, height: size + step, color }}>
      <FaCompactDisc size={size} style={{ position: "absolute", left: step, top: step, opacity: 0.55 }} />
      <FaCompactDisc size={size} style={{ position: "absolute", left: 0, top: 0, opacity: 1 }} />
    </span>
  );
};

/** One CD + its number — the "Disc N" face. */
const DiscWithNumber: FC<{ size: number; color: string; num: string }> = ({ size, color, num }) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: "4px", color }}>
    <FaCompactDisc size={size} />
    <span style={{ fontWeight: 600, fontSize: `${Math.round(size * 0.6)}px` }}>{num}</span>
  </span>
);

export const DiscSelector: FC<DiscSelectorProps> = ({ appId }) => {
  const [selection, setSelection] = useState<DiscSelection | null>(null);
  // Locally-tracked pin: `selected` echoed by a successful selectDisc. Mirrors
  // the persisted `roms.selected_disc` (null = following the default).
  const [selected, setSelected] = useState<DiscOptionData>(null);
  const romIdRef = useRef<number | null>(null);

  // Resolve rom_id from the cached detail and fetch the disc selection.
  const fetchSelection = async (rid: number): Promise<void> => {
    try {
      const result = await getDiscSelection(rid);
      setSelection(result);
      setSelected(result.selected ?? null);
    } catch (e) {
      logError(`DiscSelector: getDiscSelection failed: ${e}`);
    }
  };

  // Initial load: resolve rom_id from cache (instant), then fetch selection.
  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const cached = await getCachedGameDetail(appId);
        if (cancelled || !cached.found || cached.rom_id == null) return;
        romIdRef.current = cached.rom_id;
        if (!cached.installed) return;
        await fetchSelection(cached.rom_id);
      } catch (e) {
        logError(`DiscSelector init error: ${e}`);
      }
    }

    detach(init());
    return () => {
      cancelled = true;
    };
  }, [appId]);

  // Re-fetch on download_complete (a newly installed ROM may now be multi-disc);
  // hide on uninstall.
  useEffect(() => {
    const completeListener = addEventListener<[DownloadCompleteEvent]>(
      "download_complete",
      (evt: DownloadCompleteEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        detach(fetchSelection(evt.rom_id));
      },
    );

    const onUninstall = (e: Event) => {
      const rid = (e as CustomEvent).detail?.rom_id;
      if (rid !== romIdRef.current) return;
      setSelection(null);
      setSelected(null);
    };
    globalThis.addEventListener("romm_rom_uninstalled", onUninstall);

    return () => {
      removeEventListener("download_complete", completeListener);
      globalThis.removeEventListener("romm_rom_uninstalled", onUninstall);
    };
  }, []);

  const handleChange = async (data: DiscOptionData): Promise<void> => {
    const rid = romIdRef.current;
    if (rid == null) return;
    try {
      const result = await selectDisc(rid, data);
      if (result.success) {
        if (result.launch_options !== undefined) {
          await setLaunchOptionsConfirmed(appId, result.launch_options);
        }
        setSelected(result.selected ?? null);
      } else {
        toaster.toast({ title: "RomM Sync", body: result.message || "Failed to select disc" });
      }
    } catch (e) {
      // Observable catch effect: surface the failure so the user knows the pick
      // didn't take, and leave `selected` unchanged (revert to the prior pin).
      logError(`DiscSelector: selectDisc failed: ${e}`);
      toaster.toast({ title: "RomM Sync", body: "Failed to select disc" });
    }
  };

  // Single-disc / unknown / not-installed → render nothing.
  if (!selection?.multi_disc || !selection.discs || !selection.default) return null;

  const { discs, default: dflt } = selection;
  const isM3u = dflt.kind === "m3u";

  // The effective pin: an explicit selection, else the default target (null for
  // m3u, disc 1's filename otherwise).
  const effectiveSelected: DiscOptionData = selected ?? (isM3u ? null : dflt.filename);
  const isPinned = selected !== null;
  // The m3u playlist is active only when the default is m3u and nothing is pinned.
  const showPlaylistFace = isM3u && selected === null;
  const activeDisc = discs.find((d) => d.filename === effectiveSelected);
  const activeNum = activeDisc ? (activeDisc.label.match(/\d+/)?.[0] ?? String(activeDisc.index)) : "";

  // Options: the m3u "all discs" default (when present) followed by each disc.
  const options: { data: DiscOptionData; icon: ReactNode; text: string }[] = [];
  if (isM3u) options.push({ data: null, icon: <DiscStack size={16} color={DISC_GREY} />, text: dflt.label });
  for (const disc of discs) options.push({ data: disc.filename, icon: <FaCompactDisc size={16} />, text: disc.label });

  // A custom compact trigger + showContextMenu for the anchored list. Steam's
  // <Dropdown> renders full-width and clips a custom icon face, so we own the
  // trigger button outright (sized to its content via .romm-disc-btn). The
  // active option is tinted + check-marked in the list.
  const openMenu = (e: MouseEvent): void => {
    showContextMenu(
      <Menu label="Disc">
        {options.map((o) => {
          const active = o.data === effectiveSelected;
          return (
            <MenuItem key={String(o.data)} onClick={() => detach(handleChange(o.data))}>
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "10px",
                  color: active ? DISC_ACCENT : undefined,
                }}
              >
                {o.icon}
                <span>{o.text}</span>
                {active ? <span style={{ marginLeft: "6px", fontWeight: 700 }}>✓</span> : null}
              </span>
            </MenuItem>
          );
        })}
      </Menu>,
      getEventTarget(e),
    );
  };

  return (
    <DialogButton className="romm-disc-btn" onClick={openMenu}>
      {showPlaylistFace ? (
        <DiscStack size={22} color={DISC_GREY} />
      ) : (
        <DiscWithNumber size={22} color={isPinned ? DISC_ACCENT : DISC_GREY} num={activeNum} />
      )}
      <FaChevronDown size={10} color="#cfd3d8" />
    </DialogButton>
  );
};
