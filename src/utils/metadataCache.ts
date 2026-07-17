import { getMetadataCachePage } from "../api/backend";
import { withTimeout } from "./withTimeout";
import type { RomMetadata } from "../types";

/**
 * Page the backend metadata cache until every row is collected, assembling the
 * full ``appId → RomMetadata`` map.
 *
 * Metadata is paged so a large library never sends a multi-MB dump through the
 * size-limited WebSocket bridge in a single callable response (#1025). Each page
 * is raced against ``timeoutMs`` so a hung callable throws out of the loop — init
 * lets its retry driver restart from offset 0, the sync_complete re-apply logs
 * and moves on. The empty-page guard stops the loop even if ``total`` overshoots
 * the rows actually returned. Shared by plugin-load init and the sync_complete
 * re-apply so the pagination lives in exactly one place (#1207).
 */
export async function fetchMetadataCachePages(
  pageSize: number,
  timeoutMs: number,
): Promise<Record<string, RomMetadata>> {
  const cache: Record<string, RomMetadata> = {};
  let collected = 0;
  let total = Number.POSITIVE_INFINITY;
  let offset = 0;
  while (collected < total) {
    const page = await withTimeout(getMetadataCachePage(offset, pageSize), timeoutMs);
    total = page.total;
    const keys = Object.keys(page.items);
    if (keys.length === 0) break;
    for (const key of keys) cache[key] = page.items[key]!;
    collected += keys.length;
    offset += pageSize;
  }
  return cache;
}
