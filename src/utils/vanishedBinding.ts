/**
 * Which appIds RomM has positively confirmed it no longer serves.
 *
 * The version picker is the only surface that asks the server about a bound id
 * (`get_version_list`), and it sits beside the play button rather than above
 * it — neither can see the other's state. This is the seam between them: the
 * picker publishes what it learned, the play button reads it to stop offering a
 * download that can only ever fail.
 *
 * **Positive knowledge only.** An entry is `true` solely when the backend
 * answered `bound_vanished` for that appId, which it does exclusively on a 404
 * for the bound id. A timeout, a transport error, or an unreachable server all
 * report `false` alongside `server_query_failed`, so an offline session never
 * disables anything — the fail-open rule the whole vanished-ROM feature is
 * built on.
 */

type Listener = () => void;

const vanishedAppIds = new Map<number, boolean>();
const listeners = new Set<Listener>();

/** Record what the server said about `appId`'s bound ROM. */
export function setBoundVanished(appId: number, vanished: boolean): void {
  if ((vanishedAppIds.get(appId) ?? false) === vanished) return;
  vanishedAppIds.set(appId, vanished);
  for (const listener of listeners) listener();
}

/** Whether RomM has positively confirmed `appId`'s bound ROM is gone. */
export function isBoundVanished(appId: number): boolean {
  return vanishedAppIds.get(appId) ?? false;
}

export function onBoundVanishedChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Test seam — production never clears the map, it is keyed per appId. */
export function resetBoundVanished(): void {
  vanishedAppIds.clear();
  for (const listener of listeners) listener();
}
