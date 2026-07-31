export interface PruneProgress {
  run_id: string;
  preview_id: string;
  current: number;
  total: number;
  stage: string;
  rom_ids: number[];
  rom_count?: number;
  rom_ids_truncated?: boolean;
  name: string;
  bundle_path?: string;
}

export interface PruneGroupResult {
  group_id: string;
  group_id_truncated?: boolean;
  /** The game's display name — what a result line leads with. */
  name?: string;
  name_truncated?: boolean;
  rom_ids: number[];
  rom_count?: number;
  rom_ids_truncated?: boolean;
  status: "removed" | "repointed" | "partial" | "failed" | "skipped";
  reason?: string;
  message: string;
  message_truncated?: boolean;
  removed_rom_ids?: number[];
  removed_count?: number;
  removed_rom_ids_truncated?: boolean;
  app_id?: number;
  removed_app_id?: number;
  bundle_path?: string;
  committed_action?: "repoint_shortcut" | "remove_shortcut";
  action_ambiguous?: boolean;
  mutations?: string[];
  ambiguous_mutations?: string[];
  warnings?: string[];
  warning_count?: number;
  warnings_omitted?: boolean;
  warnings_truncated?: boolean;
  target_rom_id?: number;
}

export interface PruneComplete {
  success: boolean;
  partial: boolean;
  run_id: string;
  preview_id: string;
  chunk_index?: number;
  final?: boolean;
  removed_count?: number;
  problem_count?: number;
  publication_required?: boolean;
  prune_lease_token?: string;
  removed_rom_ids: number[];
  affected_app_ids: number[];
  removed_app_ids?: number[];
  results: PruneGroupResult[];
  reason?: string;
  message?: string;
}

type Listener = () => void;

/**
 * How long an adopted run may go without a frame before its result is presumed
 * lost. A completion is only published from a contiguous chunk set, so a single
 * dropped chunk would otherwise pin `progress` forever and permanently disable
 * the cleanup entry point. The window has to clear the longest legitimate gap
 * between frames — a whole-group recovery bundle copying and checksumming a
 * large installed ROM emits nothing while it runs.
 */
const LOST_RESULT_TIMEOUT_MS = 15 * 60_000;

let activeRunId: string | null = null;
let activePreviewId: string | null = null;
let progress: PruneProgress | null = null;
let complete: PruneComplete | null = null;
const receivedChunks = new Map<number, PruneComplete>();
let terminalChunkIndex: number | null = null;
let pendingPreviewId: string | null = null;
let resultLost = false;
let lostResultTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<Listener>();

function notify(): void {
  for (const listener of listeners) listener();
}

function disarmLostResultTimer(): void {
  if (lostResultTimer === null) return;
  clearTimeout(lostResultTimer);
  lostResultTimer = null;
}

/**
 * (Re)start the lost-result countdown from the frame just accepted.
 *
 * Firing releases the run from the UI — `progress` clears, so the entry point
 * re-enables — and raises the flag the UI turns into a "check your library and
 * scan again" warning. It deliberately keeps the half-assembled chunk set and
 * re-opens the originating preview for adoption: a run that was merely SLOW
 * re-adopts on its next frame and still finalizes, so an over-eager countdown
 * can never turn a late result into a silently dropped one.
 */
function armLostResultTimer(): void {
  disarmLostResultTimer();
  lostResultTimer = setTimeout(() => {
    lostResultTimer = null;
    if (complete !== null) return;
    pendingPreviewId = activePreviewId;
    activeRunId = null;
    progress = null;
    resultLost = true;
    notify();
  }, LOST_RESULT_TIMEOUT_MS);
}

function unique(values: number[]): number[] {
  return [...new Set(values)].sort((a, b) => a - b);
}

/**
 * Adopt a started run, and report whether the adoption took.
 *
 * A refusal means this run's frames can never be admitted (`admitPruneFrame`
 * gates on the same pending preview), so the caller has a backend run executing
 * against a UI that will never show it. That has to be surfaced, not swallowed.
 */
export function beginPruneRun(runId: string, previewId: string): boolean {
  if (activeRunId === runId) return true;
  if (pendingPreviewId !== previewId) return false;
  activeRunId = runId;
  activePreviewId = previewId;
  pendingPreviewId = null;
  progress = null;
  complete = null;
  receivedChunks.clear();
  terminalChunkIndex = null;
  resultLost = false;
  armLostResultTimer();
  notify();
  return true;
}

export function admitPruneFrame(previewId: string, runId: string): boolean {
  if (complete !== null) return false;
  if (activeRunId !== null) return activeRunId === runId;
  if (pendingPreviewId === null || pendingPreviewId !== previewId) return false;
  activeRunId = runId;
  activePreviewId = previewId;
  pendingPreviewId = null;
  resultLost = false;
  armLostResultTimer();
  notify();
  return true;
}

export function setPruneProgress(value: PruneProgress): void {
  if (!admitPruneFrame(value.preview_id, value.run_id)) return;
  progress = value;
  armLostResultTimer();
  notify();
}

export function setPruneComplete(value: PruneComplete): PruneComplete | null {
  if (!admitPruneFrame(value.preview_id, value.run_id)) return null;
  const chunkIndex = value.chunk_index ?? 0;
  if (receivedChunks.has(chunkIndex)) return null;
  receivedChunks.set(chunkIndex, value);
  if (value.final !== false) terminalChunkIndex = chunkIndex;
  armLostResultTimer();
  if (terminalChunkIndex === null) return null;
  for (let index = 0; index <= terminalChunkIndex; index++) {
    if (!receivedChunks.has(index)) return null;
  }
  disarmLostResultTimer();
  const chunks = Array.from({ length: terminalChunkIndex + 1 }, (_, index) => receivedChunks.get(index)!);
  const terminal = receivedChunks.get(terminalChunkIndex)!;
  progress = null;
  complete = {
    ...terminal,
    removed_rom_ids: unique(chunks.flatMap((chunk) => chunk.removed_rom_ids)),
    affected_app_ids: unique(chunks.flatMap((chunk) => chunk.affected_app_ids)),
    removed_app_ids: unique(chunks.flatMap((chunk) => chunk.removed_app_ids ?? [])),
    results: chunks.flatMap((chunk) => chunk.results),
  };
  notify();
  return complete;
}

export function getPruneState(): {
  runId: string | null;
  progress: PruneProgress | null;
  complete: PruneComplete | null;
} {
  return { runId: activeRunId, progress, complete };
}

/**
 * Whether the last adopted run went silent long enough that its result is
 * presumed lost. Separate from {@link getPruneState} because it survives the
 * run being dropped — it is the only trace the user gets that cleanup may have
 * done work the UI never saw.
 */
export function isPruneResultLost(): boolean {
  return resultLost;
}

export function onPruneStateChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function resetPruneState(): void {
  disarmLostResultTimer();
  activeRunId = null;
  activePreviewId = null;
  progress = null;
  complete = null;
  receivedChunks.clear();
  terminalChunkIndex = null;
  pendingPreviewId = null;
  resultLost = false;
  notify();
}

export function beginPrunePreview(previewId: string): void {
  disarmLostResultTimer();
  activeRunId = null;
  activePreviewId = null;
  progress = null;
  complete = null;
  receivedChunks.clear();
  terminalChunkIndex = null;
  pendingPreviewId = previewId;
  resultLost = false;
  notify();
}
