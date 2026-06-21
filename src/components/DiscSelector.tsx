/**
 * DiscSelector — inline disc picker for multi-disc ROMs (#865).
 *
 * Sits immediately to the right of CustomPlayButton in the play-section row.
 * For a multi-disc install it renders a compact `@decky/ui` Dropdown whose
 * button face shows the active disc (💿 {label} ▾) — the face IS the badge.
 * Picking a disc rewrites the Steam shortcut's `launch_options` to that disc's
 * file (emulator-agnostic) and persists the choice in the backend DB, so the
 * Play button always launches the currently-selected disc.
 *
 * Single-disc / unknown / not-installed ROMs render nothing (zero footprint).
 * The picker re-fetches on `download_complete` (a newly installed ROM may now
 * be multi-disc) and hides on `romm_rom_uninstalled`.
 */

import { useState, useEffect, useRef, FC } from "react";
import { addEventListener, removeEventListener, toaster } from "@decky/api";
import { Dropdown } from "@decky/ui";
import { getCachedGameDetail, getDiscSelection, selectDisc, logError } from "../api/backend";
import type { DiscSelection } from "../api/backend";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { detach } from "../utils/detach";
import type { DownloadCompleteEvent } from "../types";

interface DiscSelectorProps {
  appId: number;
}

/** A disc option's `data` value: a disc filename, or `null` for the m3u default. */
type DiscOptionData = string | null;

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

  // Build options. For an m3u default the first entry follows it (data:null,
  // "All discs (m3u)"); otherwise the options are the discs and disc 1 is the
  // default (no separate "follow default" entry).
  const rgOptions: { data: DiscOptionData; label: string }[] = [];
  if (isM3u) rgOptions.push({ data: null, label: dflt.label });
  for (const disc of discs) rgOptions.push({ data: disc.filename, label: disc.label });

  // The effective pin: an explicit selection, else the default target (null for
  // m3u, disc 1's filename otherwise).
  const effectiveSelected: DiscOptionData = selected ?? (isM3u ? null : dflt.filename);

  // The Dropdown face IS the badge: 💿 {active disc label} ▾.
  const activeLabel = rgOptions.find((o) => o.data === effectiveSelected)?.label ?? dflt.label;

  return (
    <Dropdown
      rgOptions={rgOptions}
      selectedOption={effectiveSelected}
      onChange={(opt) => {
        detach(handleChange(opt.data as DiscOptionData));
      }}
      renderButtonValue={() => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: "6px", whiteSpace: "nowrap" }}>
          <span aria-hidden="true">{"💿"}</span>
          {activeLabel}
        </span>
      )}
      menuLabel="Disc"
      focusable
    />
  );
};
