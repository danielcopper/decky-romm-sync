export interface PruneProgress {
  run_id: string;
  current: number;
  total: number;
  stage: string;
  rom_ids: number[];
  name: string;
  bundle_path?: string;
}

export interface PruneGroupResult {
  group_id: string;
  rom_ids: number[];
  status: "removed" | "failed" | "skipped";
  reason?: string;
  message: string;
  removed_rom_ids?: number[];
  app_id?: number;
  bundle_path?: string;
}

export interface PruneComplete {
  success: boolean;
  partial: boolean;
  run_id: string;
  removed_rom_ids: number[];
  affected_app_ids: number[];
  results: PruneGroupResult[];
  reason?: string;
  message?: string;
}

type Listener = () => void;
let progress: PruneProgress | null = null;
let complete: PruneComplete | null = null;
const listeners = new Set<Listener>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function setPruneProgress(value: PruneProgress): void {
  progress = value;
  complete = null;
  notify();
}

export function setPruneComplete(value: PruneComplete): void {
  progress = null;
  complete = value;
  notify();
}

export function getPruneState(): { progress: PruneProgress | null; complete: PruneComplete | null } {
  return { progress, complete };
}

export function onPruneStateChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function resetPruneState(): void {
  progress = null;
  complete = null;
  notify();
}
