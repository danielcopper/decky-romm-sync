/**
 * Per-platform accordion state — tracks which platforms are pending/applying/done
 * during a sync.  Replaces the old step/totalSteps/stepLabel fields from SyncProgress.
 *
 * Updated by syncManager.ts event handlers.
 * Read by MainPage.tsx via getAccordionState() on a polling interval.
 */

export type PlatformStatus = "pending" | "fetching" | "applying" | "done" | "partial" | "error";

export interface PlatformRow {
  name: string;
  slug: string;
  romCount: number;
  status: PlatformStatus;
  shortcutsProcessed: number;
  shortcutsTotal: number;
  currentGame?: string;
  lastArtworkBase64?: string;
}

export interface AccordionState {
  platforms: PlatformRow[];
  activePlatformIndex: number;       // -1 = none expanded
  collectionsProgress?: { current: number; total: number; label?: string };
  removalsProgress?: { current: number; total: number };
}

// ── Module-level state ────────────────────────────────────────

let _state: AccordionState = {
  platforms: [],
  activePlatformIndex: -1,
};

// ── Public functions ──────────────────────────────────────────

export function initAccordion(platforms: Array<{ name: string; slug: string; rom_count: number }>): void {
  _state = {
    platforms: platforms.map((p) => ({
      name: p.name,
      slug: p.slug,
      romCount: p.rom_count,
      status: "pending" as PlatformStatus,
      shortcutsProcessed: 0,
      shortcutsTotal: 0,
    })),
    activePlatformIndex: -1,
  };
}

export function setActivePlatform(index: number): void {
  _state.activePlatformIndex = index;
  if (index >= 0 && index < _state.platforms.length) {
    _state.platforms[index].status = "applying";
  }
}

export function updatePlatformProgress(
  name: string,
  processed: number,
  total: number,
  currentGame?: string,
): void {
  const row = _state.platforms.find((p) => p.name === name);
  if (row) {
    row.shortcutsProcessed = processed;
    row.shortcutsTotal = total;
    if (currentGame !== undefined) row.currentGame = currentGame;
  }
}

export function updatePlatformArtwork(name: string, base64: string): void {
  const row = _state.platforms.find((p) => p.name === name);
  if (row) {
    row.lastArtworkBase64 = base64;
  }
}

export function markPlatformDone(name: string): void {
  const row = _state.platforms.find((p) => p.name === name);
  if (row) {
    row.status = "done";
    row.currentGame = undefined;
  }
  // Collapse: no active platform
  if (_state.activePlatformIndex >= 0 && _state.platforms[_state.activePlatformIndex]?.name === name) {
    _state.activePlatformIndex = -1;
  }
}

export function markPlatformError(name: string): void {
  const row = _state.platforms.find((p) => p.name === name);
  if (row) {
    row.status = "error";
    row.currentGame = undefined;
  }
}

export function markPlatformPartial(name: string, processed: number, total: number): void {
  const row = _state.platforms.find((p) => p.name === name);
  if (row) {
    row.status = "partial";
    row.shortcutsProcessed = processed;
    row.shortcutsTotal = total;
    row.currentGame = undefined;
  }
}

export function updateCollectionsProgress(current: number, total: number, label?: string): void {
  _state.collectionsProgress = { current, total, label };
}

export function updateRemovalsProgress(current: number, total: number): void {
  _state.removalsProgress = { current, total };
}

export function getAccordionState(): AccordionState {
  return _state;
}

export function resetAccordion(): void {
  _state = {
    platforms: [],
    activePlatformIndex: -1,
  };
}
