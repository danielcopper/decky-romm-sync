export interface PruneProgress {
  run_id: string;
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
  warnings_truncated?: boolean;
  target_rom_id?: number;
}

export interface PruneComplete {
  success: boolean;
  partial: boolean;
  run_id: string;
  chunk_index?: number;
  final?: boolean;
  removed_count?: number;
  problem_count?: number;
  removed_rom_ids: number[];
  affected_app_ids: number[];
  removed_app_ids?: number[];
  results: PruneGroupResult[];
  reason?: string;
  message?: string;
}

type Listener = () => void;
let activeRunId: string | null = null;
let progress: PruneProgress | null = null;
let complete: PruneComplete | null = null;
const receivedChunks = new Map<number, PruneComplete>();
let terminalChunkIndex: number | null = null;
const listeners = new Set<Listener>();

function notify(): void {
  for (const listener of listeners) listener();
}

function unique(values: number[]): number[] {
  return [...new Set(values)].sort((a, b) => a - b);
}

export function beginPruneRun(runId: string): void {
  if (activeRunId === runId) return;
  activeRunId = runId;
  progress = null;
  complete = null;
  receivedChunks.clear();
  terminalChunkIndex = null;
  notify();
}

export function setPruneProgress(value: PruneProgress): void {
  if (activeRunId !== null && activeRunId !== value.run_id) return;
  activeRunId = value.run_id;
  progress = value;
  complete = null;
  notify();
}

export function setPruneComplete(value: PruneComplete): PruneComplete | null {
  if (activeRunId !== null && activeRunId !== value.run_id) return null;
  activeRunId = value.run_id;
  const chunkIndex = value.chunk_index ?? 0;
  if (receivedChunks.has(chunkIndex)) return null;
  receivedChunks.set(chunkIndex, value);
  if (value.final !== false) terminalChunkIndex = chunkIndex;
  if (terminalChunkIndex === null) return null;
  for (let index = 0; index <= terminalChunkIndex; index++) {
    if (!receivedChunks.has(index)) return null;
  }
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

export function onPruneStateChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function resetPruneState(): void {
  activeRunId = null;
  progress = null;
  complete = null;
  receivedChunks.clear();
  terminalChunkIndex = null;
  notify();
}
