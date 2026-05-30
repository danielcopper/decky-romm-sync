/**
 * SgdbGamePickerModal — the manual resolution surface for the SGDB artwork
 * cascade. Opened by RomMPlaySection's "Refresh Artwork" action only when the
 * backend can resolve no SteamGridDB game id at all (RomM has no sgdb_id and
 * the IGDB cross-ref yields nothing).
 *
 * A search field (prefilled with the ROM name) lets the user search SGDB and
 * pick any result. Selecting a result persists the id via applySgdbGameId,
 * re-runs the artwork apply for the appId, then reports the applied count back
 * and closes. The pick is not protected — a later sync with a RomM sgdb_id
 * overwrites it.
 *
 * Wrapped in Steam's focus-managed ConfirmModal (same pattern as
 * TextInputModal / NewSlotModal) so the on-screen keyboard and d-pad / left
 * stick navigation behave correctly: focus flows field-row → results grid and
 * back out. The modal's single OK button only dismisses — searching is an
 * explicit in-body Search button (and R2 / TRIGGER_RIGHT shortcut), never the
 * OK action, which would close the modal before the user can pick.
 */

import { FC, useState } from "react";
import {
  ConfirmModal,
  DialogButton,
  Focusable,
  GamepadButton,
  TextField,
  Spinner,
  type GamepadEvent,
} from "@decky/ui";
import { toaster } from "@decky/api";
import {
  searchSgdbGames,
  applySgdbGameId,
  debugLog,
  type SgdbCandidate,
} from "../api/backend";
import { applyArtwork } from "../utils/artwork";
import { scrollToTop, scrollFocusedToCenter } from "../utils/scrollHelpers";

export interface SgdbGamePickerModalProps {
  romId: number;
  appId: number;
  romName: string;
  /** Initial candidate list from the resolution cascade. */
  candidates?: SgdbCandidate[];
  /** Reports how many images applyArtwork applied (or -1 for no API key). */
  onApplied: (appliedCount: number) => void;
  /** Injected by showModal. */
  closeModal?: () => void;
}

/** Selectable tile showing a thumbnail (or placeholder) plus optional subtitle. */
const Tile: FC<{
  thumbUrl: string | null;
  title: string;
  subtitle?: string;
  onSelect: () => void;
  onFocus?: (e: { currentTarget: EventTarget | null }) => void;
  disabled?: boolean;
}> = ({ thumbUrl, title, subtitle, onSelect, onFocus, disabled }) => (
  <DialogButton
    onClick={onSelect}
    onFocus={onFocus}
    disabled={disabled}
    style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      gap: "6px",
      padding: "8px",
      width: "100%",
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
  candidates,
  onApplied,
  closeModal,
}) => {
  const [term, setTerm] = useState(romName);
  const [results, setResults] = useState<SgdbCandidate[]>(candidates ?? []);
  const [searching, setSearching] = useState(false);
  const [applying, setApplying] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

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
      if (res.success) {
        setResults(res.games);
        if (res.games.length === 0) {
          setSearchError("No matches found.");
        }
      } else {
        setSearchError("Search failed. Check your connection and try again.");
        setResults([]);
      }
    } finally {
      setSearching(false);
    }
  };

  const applySelection = async (selectedId: number) => {
    if (applying) return;
    setApplying(true);
    try {
      const result = await applySgdbGameId(romId, selectedId).catch(
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

  // R2 (TRIGGER_RIGHT) on the focus-managed body triggers a search without
  // leaving the field. The OSK is modal and captures input while open, so this
  // only fires when focus is on the body/field with the keyboard closed —
  // acceptable; the structural nav fix is the primary goal.
  const onBodyButtonDown = (evt: GamepadEvent) => {
    if (evt.detail.button === GamepadButton.TRIGGER_RIGHT) {
      void runSearch();
    }
  };

  return (
    <ConfirmModal
      closeModal={closeModal}
      onOK={() => closeModal?.()}
      onCancel={() => closeModal?.()}
      strTitle="Choose SteamGridDB Game"
      strOKButtonText="Close"
      bAlertDialog={true}
    >
      <Focusable
        flow-children="column"
        onButtonDown={onBodyButtonDown}
        onOKActionDescription="Select"
        actionDescriptionMap={{ [GamepadButton.TRIGGER_RIGHT]: "Search" }}
        style={{ display: "flex", flexDirection: "column", gap: "12px", width: "100%" }}
      >
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.6)" }}>{romName}</div>
        <div style={{ fontSize: "12px", color: "rgba(255,255,255,0.5)" }}>
          No SteamGridDB match was found automatically — search by name and pick the right game.
        </div>

        <Focusable
          flow-children="row"
          style={{ display: "flex", gap: "8px", alignItems: "center" }}
        >
          <div style={{ flex: 1 }}>
            <TextField
              focusOnMount={true}
              label="Search SteamGridDB"
              value={term}
              onChange={(e: { target: { value: string } }) => setTerm(e.target.value)}
              onFocus={scrollToTop}
            />
          </div>
          <DialogButton
            onClick={() => { void runSearch(); }}
            onFocus={scrollToTop}
            disabled={searching}
            style={{ width: "120px", height: "40px" }}
          >
            Search
          </DialogButton>
        </Focusable>

        {searching ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "16px" }}>
            <div style={{ width: "32px", height: "32px" }}>
              <Spinner />
            </div>
          </div>
        ) : null}

        {searchError ? (
          <div style={{ fontSize: "12px", color: "#ff8800" }}>{searchError}</div>
        ) : null}

        {results.length > 0 ? (
          <div style={{ fontSize: "11px", color: "rgba(255,255,255,0.5)" }}>
            Showing the top 6 matches — refine your search if the right game isn&apos;t here.
          </div>
        ) : null}

        {results.length > 0 ? (
          <Focusable
            style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px" }}
            flow-children="right"
          >
            {results.map((game) => (
              <Tile
                key={game.id}
                thumbUrl={game.thumb_url}
                title={game.name}
                subtitle={game.release_year == null ? undefined : String(game.release_year)}
                onSelect={() => { void applySelection(game.id); }}
                onFocus={scrollFocusedToCenter}
                disabled={applying}
              />
            ))}
          </Focusable>
        ) : null}
      </Focusable>
    </ConfirmModal>
  );
};
