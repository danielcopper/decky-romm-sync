/**
 * Race a promise against a deadline. Resolves/rejects with `promise` when it
 * settles first; rejects with a timeout error once `ms` elapses. The deadline
 * timer is cleared as soon as the race settles, so a settled call never leaves a
 * pending timer behind.
 *
 * Decky's `callable()` has no timeout of its own and hangs forever when the
 * backend isn't ready — every callable probe that must stay responsive races
 * through this.
 */
export function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer!: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`callable timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}
