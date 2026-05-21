/**
 * SgdbGamePickerModal — the manual resolution surface for the SGDB artwork
 * cascade. Opened by RomMPlaySection's "Refresh Artwork" action whenever the
 * backend can't pick a SteamGridDB game id on its own:
 *
 * - conflict — the stored id and RomM's id disagree; both are shown as
 *   "Current" / "From RomM" tiles for the user to choose between.
 * - needs_pick — nothing resolved; a name-search picker is offered.
 *
 * Whatever the entry point, a search field (prefilled with the ROM name) lets
 * the user search SGDB and pick any result. Selecting a tile persists the id
 * via applySgdbGameId, re-runs the artwork apply for the appId, then reports
 * the applied count back and closes.
 */

import { FC, useState } from "react";
import { ModalRoot, DialogButton, Focusable, TextField, Spinner } from "@decky/ui";
import { toaster } from "@decky/api";
import {
  searchSgdbGames,
  applySgdbGameId,
  debugLog,
  type SgdbCandidate,
} from "../api/backend";
import { applyArtwork } from "../utils/artwork";

export interface SgdbGamePickerModalProps {
  romId: number;
  appId: number;
  romName: string;
  /** Conflict entry point: the stored ("Current") tile. */
  stateTile?: { id: number; thumb_url: string | null };
  /** Conflict entry point: the RomM tile. */
  rommTile?: { id: number; thumb_url: string | null };
  /** needs_pick entry point: initial candidate list. */
  candidates?: SgdbCandidate[];
  /** Reports how many images applyArtwork applied (or -1 for no API key). */
  onApplied: (appliedCount: number) => void;
  /** Injected by showModal. */
  closeModal?: () => void;
}

/** Selectable tile showing a thumbnail (or placeholder) plus optional label. */
const Tile: FC<{
  thumbUrl: string | null;
  title: string;
  subtitle?: string;
  onSelect: () => void;
  disabled?: boolean;
}> = ({ thumbUrl, title, subtitle, onSelect, disabled }) => (
  <DialogButton
    onClick={onSelect}
    disabled={disabled}
    style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      gap: "6px",
      padding: "8px",
      width: "140px",
      height: "auto",
      minWidth: "0",
    }}
  >
    {thumbUrl ? (
      <img
        src={thumbUrl}
        alt={title}
        style={{ width: "120px", height: "68px", objectFit: "cover", borderRadius: "4px" }}
      />
    ) : (
      <div
        style={{
          width: "120px",
          height: "68px",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "rgba(255,255,255,0.08)",
          borderRadius: "4px",
          fontSize: "11px",
          color: "rgba(255,255,255,0.5)",
        }}
      >
        No preview
      </div>
    )}
    <div style={{ fontSize: "12px", color: "#fff", textAlign: "center", lineHeight: "1.2" }}>
      {title}
    </div>
    {subtitle ? (
      <div style={{ fontSize: "11px", color: "rgba(255,255,255,0.55)" }}>{subtitle}</div>
    ) : null}
  </DialogButton>
);

export const SgdbGamePickerModalContent: FC<SgdbGamePickerModalProps> = ({
  romId,
  appId,
  romName,
  stateTile,
  rommTile,
  candidates,
  onApplied,
  closeModal,
}) => {
  const [term, setTerm] = useState(romName);
  const [results, setResults] = useState<SgdbCandidate[]>(candidates ?? []);
  const [searching, setSearching] = useState(false);
  const [applying, setApplying] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const hasConflict = stateTile != null && rommTile != null;

  const runSearch = async () => {
    if (searching) return;
    setSearching(true);
    setSearchError(null);
    try {
      const res = await searchSgdbGames(term).catch(
        (e): { success: boolean; games: SgdbCandidate[] } => {
          debugLog(`SgdbGamePickerModal: searchSgdbGames rejected: ${e}`);
          return { success: false, games: [] };
        },
      );
      if (!res.success) {
        setSearchError("Search failed. Check your connection and try again.");
        setResults([]);
      } else {
        setResults(res.games);
        if (res.games.length === 0) {
          setSearchError("No matches found.");
        }
      }
    } finally {
      setSearching(false);
    }
  };

  const applySelection = async (selectedId: number, source: string) => {
    if (applying) return;
    setApplying(true);
    try {
      const result = await applySgdbGameId(romId, selectedId, source).catch(
        (e): { success: boolean } => {
          debugLog(`SgdbGamePickerModal: applySgdbGameId rejected: ${e}`);
          return { success: false };
        },
      );
      if (!result.success) {
        toaster.toast({ title: "RomM Sync", body: "Failed to apply artwork selection" });
        return;
      }
      const applied = await applyArtwork(romId, appId).catch((e): number => {
        debugLog(`SgdbGamePickerModal: applyArtwork rejected: ${e}`);
        return 0;
      });
      if (applied === -1) {
        toaster.toast({ title: "RomM Sync", body: "Set a SteamGridDB API key in settings first" });
      } else if (applied > 0) {
        toaster.toast({ title: "RomM Sync", body: `Artwork refreshed (${applied}/4 images applied)` });
      } else {
        toaster.toast({ title: "RomM Sync", body: "No artwork available for this game" });
      }
      onApplied(applied);
      closeModal?.();
    } finally {
      setApplying(false);
    }
  };

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ padding: "16px", minWidth: "480px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", marginBottom: "4px", color: "#fff" }}>
          Choose SteamGridDB Game
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.6)", marginBottom: "16px" }}>
          {romName}
        </div>

        {hasConflict ? (
          <div style={{ marginBottom: "16px" }}>
            <div style={{ fontSize: "13px", color: "#fff", marginBottom: "8px" }}>
              Two different matches were found. Pick the artwork you want:
            </div>
            <Focusable
              style={{ display: "flex", gap: "12px", justifyContent: "center" }}
              flow-children="right"
            >
              <Tile
                thumbUrl={stateTile!.thumb_url}
                title="Current"
                onSelect={() => applySelection(stateTile!.id, "keep")}
                disabled={applying}
              />
              <Tile
                thumbUrl={rommTile!.thumb_url}
                title="From RomM"
                onSelect={() => applySelection(rommTile!.id, "romm")}
                disabled={applying}
              />
            </Focusable>
          </div>
        ) : null}

        <div style={{ display: "flex", gap: "8px", alignItems: "flex-end", marginBottom: "12px" }}>
          <div style={{ flex: 1 }}>
            <TextField
              label="Search SteamGridDB"
              value={term}
              onChange={(e: { target: { value: string } }) => setTerm(e.target.value)}
            />
          </div>
          <DialogButton onClick={runSearch} disabled={searching} style={{ width: "120px" }}>
            Search
          </DialogButton>
        </div>

        {searching ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "16px" }}>
            <Spinner />
          </div>
        ) : null}

        {searchError ? (
          <div style={{ fontSize: "12px", color: "#ff8800", marginBottom: "8px" }}>
            {searchError}
          </div>
        ) : null}

        {results.length > 0 ? (
          <Focusable
            style={{ display: "flex", flexWrap: "wrap", gap: "12px", justifyContent: "flex-start" }}
            flow-children="right"
          >
            {results.map((game) => (
              <Tile
                key={game.id}
                thumbUrl={game.thumb_url}
                title={game.name}
                subtitle={game.release_year != null ? String(game.release_year) : undefined}
                onSelect={() => applySelection(game.id, "manual")}
                disabled={applying}
              />
            ))}
          </Focusable>
        ) : null}
      </div>
    </ModalRoot>
  );
};
