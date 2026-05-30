# Narrow Unit-of-Work scope — transactions wrap database I/O only, never network/file/UI waits

## Status

Accepted. Refines [ADR-0004](0004-sync-sqlite-unit-of-work.md), which set the
sync-`sqlite3` + `run_in_executor` UoW but left the transaction *scope* open.

## Context

SQLite WAL allows many concurrent readers but exactly **one writer for the whole
database**; `busy_timeout=5000` makes a second writer wait up to 5s and then fail
with `SQLITE_BUSY`. Every heavy backend operation runs in the default thread
pool, so two DB-writing operations genuinely overlap on different threads. The
routine case — established by investigation, not assumed — is a user pressing
**Play on a game (which fires `pre_launch_sync`, writing save tables) during a
background library sync** (writing `roms` / `rom_metadata` / `sync_runs`); the
two are ungated and overlap every session a sync is running.

A naïve "open the UoW at the worker's entry, do everything inside, commit at
exit" would hold the single write transaction across the operation's server
fetches, multi-second file downloads/uploads, and — for library sync — the
up-to-60s wait for the frontend to ack a unit. That stalls every other writer
for the whole I/O duration and turns any >5s hold into a hard `SQLITE_BUSY`
error on routine concurrency. The existing JSON code already avoids this by
mutating an in-memory object during the I/O and persisting **once at the end**.

## Decision

A Unit of Work wraps **only the database reads and writes — never network I/O,
file I/O, or a frontend round-trip.** An operation that interleaves I/O opens a
**short read UoW** (load the aggregates it needs), performs its I/O **outside any
transaction** (mutating the loaded aggregates in memory), then opens a **short
write UoW** (persist). A simple mutation with no I/O is one short `with uow:`.

Cross-operation consistency is provided by the operation's **own serialization**
— the per-ROM `asyncio.Lock` for saves, the single background task for library
sync — **not** by holding a UoW open. The per-ROM lock therefore survives the
cutover unchanged: it is the *logical* serializer (it guards the whole
read→fetch→transfer→write sequence, which a DB transaction cannot), orthogonal
to the UoW, which is merely the *atomic DB write*. No single dedicated DB-writer
thread and no application-level DB locks are introduced.

## Consequences

- Some workers open the UoW twice (read, then write) with I/O between. This is
  the same shape the JSON code already had (load state → do I/O → persist once),
  not new ceremony.
- A read-then-write window exists between the two short UoWs; it is closed by the
  operation's lock / single-task serialization, not by the database. Safe because
  no other operation mutates the same aggregate concurrently.
- No write transaction is ever held across slow I/O, so the routine
  "launch during sync" overlap cannot produce `SQLITE_BUSY` — narrow writes run
  in tens of milliseconds, and even a bulk per-unit registry commit is
  sub-second, far under `busy_timeout`.

## Alternatives considered

- **Wide UoW** (one `with uow:` around the whole operation). Rejected: holds the
  single WAL writer across network/file/ack I/O, stalling all other writers and
  raising `SQLITE_BUSY` on routine concurrency. Simpler to write, wrong for a
  single-writer DB shared by overlapping operations.
- **Single dedicated DB-writer thread** (serialize all DB access through one
  executor). Rejected: it does not remove the need to keep transactions off slow
  I/O — a wide transaction on the single thread just queues everyone for the I/O
  duration — and it adds a second executor hop to every DB call. Unnecessary for
  a single-user plugin where narrow writes are already ~tens of ms.
