# Sync `sqlite3` (not `aiosqlite`) for the runtime Unit of Work

## Status

Accepted. **Reverses the runtime-UoW connection decision recorded in epic
[#271](https://github.com/danielcopper/decky-romm-sync/issues/271)** ("fresh
`aiosqlite.connect()` per UoW … Async chosen over sync+executor"), which was a
planning-session decision never exercised in code.

## Context

Epic #271 locked the runtime Unit-of-Work on **async `aiosqlite`** (vendored),
having flipped from an initial sync recommendation during the design session.
The two recorded drivers were: ~150–250 LOC less service boilerplate (no
`run_in_executor` shim per DB-touching method) and a uniform
`async def + async with uow` shape (one indirection step instead of two —
better traceability). It was explicitly **not** chosen for performance. The
decision was never implemented — `aiosqlite` was never vendored, and the only
persistence code that has landed (the [#781](https://github.com/danielcopper/decky-romm-sync/issues/781)
migration runner) is stdlib `sqlite3`.

Re-examined before implementing
[#783](https://github.com/danielcopper/decky-romm-sync/issues/783), with three
findings:

- **There is no performance or correctness dimension.** `aiosqlite` is itself
  thread-based — it runs one dedicated worker thread plus a queue and per-call
  futures *per connection*. It is a friendlier async *surface* over the same
  blocking `sqlite3`, not async I/O. Both `aiosqlite` and `sqlite3` +
  `run_in_executor` keep the event loop responsive identically (each offloads
  the blocking call to a thread). For a single-user, single-writer,
  microsecond-local-query workload, WAL's reader/writer concurrency — the one
  structural feature `aiosqlite`'s serialized worker model could expose — has
  nothing to exploit. The epic already knew this ("not for speed"); confirming
  it removes any lingering "async is more correct under asyncio" intuition.
- **A sync precedent now exists in the code.** The merged #781 migration runner
  is stdlib `sqlite3`. Sync for the runtime UoW is consistent with it; async
  would split the persistence layer across two paradigms.
- **`run_in_executor` is the house idiom.** The `do_<verb>` / `_<verb>_io` +
  `loop.run_in_executor` pattern is used pervasively across services (CLAUDE.md
  "Async/sync method naming"). It is already reviewed and familiar, and the UoW
  slots straight into it. `aiosqlite` would introduce a *second* way to do I/O.

## Decision

The runtime Unit-of-Work and Repository adapters use **stdlib `sqlite3`
(synchronous)**. No `aiosqlite`; no new vendored dependency.

- The UoW is a **synchronous context manager** that opens one `sqlite3`
  connection per operation, applies the runtime PRAGMAs, exposes the nine
  repositories, and commits / rolls back on exit.
- Services call it from their `async def` callables via the existing
  `run_in_executor` idiom — the `with uow_factory() as uow:` block runs inside
  the synchronous `_<verb>_io` / `do_<verb>` worker.
- **Thread affinity** (a `sqlite3` connection is single-thread by default,
  `check_same_thread=True`) is handled by opening and closing the connection
  *inside* the executor call, so the connection never escapes its worker
  thread — matching the existing `_..._io` pattern. `check_same_thread` is left
  at its safe default.

Unchanged from epic #271 (these were never the contested part): connection
**per UoW**; **service-scoped** UoW lifecycle (thin `main.py` callables don't
see the factory); **one UoW per platform unit** for library sync (a crash loses
only the in-flight unit); repositories **return domain aggregates**, not
TypedDicts. Runtime per-connection PRAGMAs: `foreign_keys=ON`,
`synchronous=NORMAL`, `busy_timeout=5000`, `temp_store=MEMORY`,
`isolation_level=None` (the UoW issues explicit `BEGIN`/`COMMIT`/`ROLLBACK`);
`journal_mode=WAL` is persistent and already set by the #781 runner.

## Consequences

- The ~150–250 LOC of `run_in_executor` boilerplate the epic sought to avoid is
  accepted as the cost — but it is the *already-familiar* house idiom, not new
  cognitive load, and each per-method shim nests into the `_io` worker services
  already use.
- No `aiosqlite` under `_vendor/` — one fewer third-party surface to patch and
  track in a plugin that has no package manager.
- The epic body's "Connection pattern" bullet is superseded by this ADR.
  `#783`'s body had drifted the other way (`sqlite3` but `synchronous=FULL` and
  TypedDict returns); the live values are `synchronous=NORMAL` (the epic and the
  `001_initial.sql` DDL comment already say NORMAL — `FULL` was a stale
  copy-error) and aggregate returns.

## Not a one-way door

If a second concurrent writer or process is introduced (e.g. a CLI or web
consumer alongside the plugin — also the chapter-8 domain-events trigger), or a
long-lived connection held across many awaits becomes the natural design,
`aiosqlite`'s by-construction thread-affinity safety could pay for itself.
Revisit then, with real call sites. None of those conditions hold today.

## Alternatives considered

- **Async `aiosqlite` (the epic's locked choice).** Rejected on re-examination:
  no performance or concurrency gain for this workload (it is thread-based
  too), it requires vendoring a dependency, and it introduces a second I/O
  paradigm alongside the established `run_in_executor` idiom and the sync #781
  runner. Its one genuine advantage — thread-affinity safety by construction —
  is cheaply matched by connection-per-operation inside the executor. The
  boilerplate-and-traceability case for it is real but does not outweigh the
  dependency-plus-paradigm-split cost.
