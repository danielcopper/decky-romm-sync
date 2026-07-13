# The apply measures Steam's renderer RSS and pauses before the session budget is exhausted

## Status

Accepted. Tracked under [#1383](https://github.com/danielcopper/decky-romm-sync/issues/1383). Builds directly on
[ADR-0023](0023-chunked-per-unit-apply.md): chunking made hitting the renderer's memory ceiling a _cheap resume_ instead
of catastrophic loss, but it did not stop the run from hitting it. This decision adds the missing half — a measurement
that lets the run stop _before_ the crash, at a chunk boundary, as a controlled resumable pause.

## Context

Steam's `SharedJSContext` renderer (a child of `steamwebhelper`) has a hard per-session heap budget. Two on-device test
days (2026-07-10/11) quantified it:

- **The cliff.** The renderer OOM-crashes at roughly 2.45–2.53 GB RSS — the observed crash cluster was 2456 / 2489 /
  2514–2516 / 2528 MB, all within ~2% of each other.
- **The cost.** Each _created_ shortcut costs 0.7–1.5 MB of RSS permanently. The rate is constant within one client boot
  but varies between boots (a 2026-07-11 boot measured 0.64–0.8). Updates and removals contribute too, but less.
- **No self-recovery.** The budget does not come back within a session — one small GC tranche after a run, then a stable
  floor. Only a Steam client restart resets it, to a fresh ~400–440 MB baseline.

So a large first import is a per-boot coin flip: chunking (ADR-0023) guarantees a cheap resume _after_ a crash, but a
crash still kicks the user out of Big Picture / the game they were in, and on a bad boot the resume can crash again. The
crash is avoidable if the run can see how close it is to the cliff and stop itself.

Two measurement facts make that feasible:

- **RSS is readable from `/proc`.** The `SharedJSContext` renderer is the **maximum-RSS** `steamwebhelper` process
  (validated 2026-07-11: it grew 438 → 2528 MB across a sync, crashed at the cliff, and respawned at the baseline).
  Filtering by cmdline `--type=renderer` is impossible — renderers are zygote-forked and inherit the zygote's cmdline —
  so the max-RSS heuristic is the discriminator. It only ever misidentifies toward a _larger_ reading, which pauses
  slightly early (harmless), never late.
- **A GC settles the reading.** Steam's natural GC is unreliable (measured: sometimes 7 minutes, sometimes absent for
  12+ minutes), so a raw reading includes transient garbage. An explicit `HeapProfiler.collectGarbage` over the Chrome
  DevTools Protocol reclaims it deterministically (measured: 496 MB in ~5 s). CEF remote debugging on `localhost:8080`
  is a Decky platform invariant (Decky Loader requires it), and NSLGameScanner is production precedent for driving it.

## Decision

**Before emitting each apply chunk, force a renderer GC, measure the renderer's RSS, and pause the run at that chunk
boundary if applying the chunk would cross the session budget. Thresholds are RSS-based, never game counts. Every step
is fail-open — a measurement failure never blocks a sync.**

- **One measurement infrastructure, three consumers.** A `/proc` RSS reader (`adapters/renderer_rss.py`,
  `RendererRssFn`) and a CDP garbage-collect trigger (`adapters/renderer_gc.py`, `RendererGcFn`) feed a pure decision
  kernel (`domain/session_budget.py`). The kernel holds the measured constants with provenance and three functions:
  `gate_decision` (per-chunk pause), `predict_run_crosses` (post-preview prognosis), `post_run_advisory` (post-run
  restart nudge). It takes plain integers and returns plain values; `None`-handling stays in the caller.
- **The gate is at every chunk boundary, GC-before-measure — but the GC is skipped when it can't matter.** In the
  ADR-0023 chunk loop (`sync_orchestrator.py::_apply_unit_in_chunks`), at each chunk boundary: read RSS (fail-open),
  fire the GC (fail-open) to settle the reading, re-read, and pause if the projection crosses `cliff − margin` (≈2.2 GB
  — the margin is 250 MB, widened from 150 MB after on-device observation 2026-07-12 that near V8's heap limit the
  renderer enters aggressive GC thrash and the UI turns sluggish _before_ it crashes; pausing 250 MB below the cliff
  keeps a chunk's transient peak out of that zone). The projection is worst-case (every item priced as a fresh create)
  so the gate errs toward pausing early — a false pause costs a Steam restart, a false proceed costs a renderer crash
  mid-apply. The chunk boundary is the only decision point because it is the only place the run can stop cleanly and
  durably. **GC-skip below a floor:** the ~5 s GC only earns its cost near the ceiling, so the measure reads RSS raw
  first and, when that raw reading is already below `GC_SKIP_BELOW_KB` (1.5 GB), returns it directly with no GC — a raw
  reading still holds transient garbage, so the settled value can only be lower, and below that floor even the
  worst-case max chunk (1.5 + 0.5 = 2.0 < 2.2 GB ceiling; 1.5 < 1.8 GB advisory) clears every threshold. Small syncs
  therefore pay zero GC cost; only a run genuinely approaching the cliff pays for the settle.
- **The first chunk is gated predictively against the cliff, not the ceiling.** Both modes are predictive
  (`rss + chunk_items × worst_case_rate ≥ limit`); they differ only in the `limit` line. Every _later_ chunk projects
  against `cliff − margin` (≈2.2 GB), keeping the anti-thrash safety margin. The run's very _first_ chunk projects
  against `CLIFF_KB` (≈2.45 GB) instead: forward progress must be guaranteed (the run has to apply at least one chunk or
  it loops forever on a no-progress pause), so that one chunk is allowed to spend _into_ the safety margin — but the
  predictive projection still stops it before the crash line itself. Net effect: a resume's first chunk proceeds only
  when its worst-case peak stays below the cliff (≈2.15 GB for a full 200-item chunk) and can never be projected past
  it; at/above that it re-pauses with zero progress and the banner directs the user to restart. After a real Steam
  restart (baseline ~430 MB) the first-chunk check never fires; a resume attempted _without_ a restart re-pauses cleanly
  and tells the user to restart — the run cannot cross the cliff on its own.
- **A pause is a first-class `paused` run status, reusing ADR-0023's stop mechanics.** The gate sets `run_paused` + a
  distinct `interrupt_reason` and requests cancel; the chunk loop returns cleanly with prior chunks committed, and the
  terminal `SyncRun` write records the new terminal status **`paused`** (migration 014 widens the `sync_runs` status
  CHECK). `paused` is deliberately its own status, distinct from `interrupted`: `interrupted` is an external death (a
  frontend crash / backend restart), `paused` is the gate's own consented stop. Both are resumable — everywhere
  `interrupted` reads as resumable (`canResume`, `get_latest_terminal`, the "last attempt" line) `paused` joins it — but
  the split lets the UI say "(paused)" with restart-and-resume guidance rather than reusing the crash wording. Completed
  platforms keep their `PlatformSyncState` stamps, so Resume Sync redoes only the remainder. The distinct reason ("Sync
  paused: Steam's memory is nearly full. Restart Steam when convenient, then Resume Sync.") rides the `sync_complete`
  payload so the toast and QAM status read the resume-friendly guidance.
- **Two advisories, RSS-based, no forced action.** After the **preview**, `predict_run_crosses` projects the run's real
  work — new creates at the worst-case create rate plus changed updates at the lighter Set*-walk rate (~1 MB/item);
  fully-unchanged items are not priced, so a large unchanged re-sync never warns — and drives a **blue/info** hint (it
  announces normal, planned behavior) that the sync will likely pause and can be resumed. After a **clean run**,
  `post_run_advisory` (RSS > ~1.8 GB, read GC-first for the same settled-heap consistency) recommends a Steam restart.
  Both are informational; consent stays with the user.
- **Persistent QAM banners with live numbers.** Toasts are missed and truncate, so the durable surfaces are two
  persistent banners in the QAM sync section, fed by a new `get_session_budget_status()` callable (a live `/proc` RSS
  read — no GC — plus the fixed ceiling/cliff lines; fail-open `rss_kb: null`). A **blue** banner shows while the last
  run is `paused` ("restart Steam, then Resume Sync" + "Steam memory: X.X GB …"); a **yellow** banner shows whenever the
  live RSS exceeds ~1.8 GB after a completed run (it self-clears after a restart, since the next read is low — no
  dismissed-state to persist). Both drop the number but keep their text when `rss_kb` is null. The pause toast stays for
  immediacy (with a longer duration so it isn't truncated), but the banners are the source of truth.
- **The blue paused banner notices a restart.** The live reading decides — no flag, no persisted state. The callable
  also returns `resume_ready` (`domain.session_budget.resume_would_proceed`:
  `rss + RESUME_HEADROOM_CHUNKS × FULL_CHUNK_WORST_KB < ceiling`, i.e. `rss < ~1.2 GB`; `None` when RSS is unreadable —
  headroom for TWO worst-case chunks, not the gate's one: a one-chunk bar sits exactly on the pause point, where Steam's
  own small frees flicker the verdict and mislabel a still-pinned heap as free, observed on-device). When it flips
  `true` — e.g. after a Steam restart drops RSS to the fresh baseline — the blue banner changes to "Steam memory is free
  again (X.X GB) — press Resume Sync" and hides the restart button; `false`/`null` keeps the restart guidance
  (conservative fail-open). Because the poll runs only during a sync, the QAM also polls the callable every ~10 s while
  a paused banner is showing, so the flip happens on its own after the user restarts — the user isn't left staring at
  stale "restart Steam" copy over a fresh green reading.
- **An always-on memory row with the last-run delta.** The QAM status block carries a permanent Steam memory row (the
  same live reading, omitted entirely when `rss_kb` is null) with the last run's signed RSS growth appended inline on
  the same line ("X.X GB · last run +Y", the delta's GB unit dropped as redundant next to the reading) — `end - start`,
  measured at EVERY terminal (completed, paused, cancelled, interrupted) so a paused run reads honestly as _that_ run's
  consumption-so-far (~+1.5 GB) rather than leaving a prior clean run's number. A **raw** read taken at run start
  (captured unconditionally in the run-scoped box, so even a fully-incremental-skip run that applies nothing still
  records a baseline and reports ≈ +0.0 GB) is the start; the terminal RSS read is the end. The delta is an
  approximation for information only — a raw start baseline (which may hold transient garbage) is fine for it. The
  signed value is retained in memory so `get_session_budget_status` returns it on a QAM remount (`memory_delta_kb`;
  in-memory only — a plugin reload loses it, no migration); the UI reads it from that callable, so it is deliberately
  NOT put on the `sync_complete` wire. It degrades to no delta segment whenever either endpoint was unmeasurable, so a
  stale number is never shown. The value text is traffic-light coloured — green below the advisory floor, yellow
  at/above it (`warn_kb`, the same line as the yellow banner), red at/above the pause ceiling (`ceiling_kb`) — and all
  three thresholds ride the `get_session_budget_status` payload so the frontend holds no threshold magic numbers. While
  a sync is running the row polls the callable every ~5 s so the number (and its colour) track the climbing RSS instead
  of the stale mount-time reading.
- **"Restart Steam now" — a deterministic full client restart, not a renderer reload.** Both banners carry a **Restart
  Steam now** button that calls `SteamClient.User.StartRestart(false)` directly from the frontend (no backend callable).
  A full Steam client restart deterministically resets the renderer's per-session heap budget to the ~430 MB baseline.
  It **replaces** an earlier `Page.reload` "free Steam memory" mechanism that proved non-deterministic on-device
  (2026-07-12): Steam sometimes rebuilds the whole page family and the OLD renderer generation lingers (a ~2.2 GB
  process hosting the previous `uid2` page generation), so total footprint went UP and the UI stayed sluggish — the
  reload could not be relied on to free memory. `StartRestart` is fire-and-forget FE-side (the restart tears the whole
  client down and back up). The button is **disabled while a game is running** and hard-guarded on click
  (`isAnyAppRunning`, reusing the existing running-app detection) so a restart can NEVER close a running game — the
  guard covers the race where a game starts between render and click. This keeps the "no forced restarts — explicit
  consent click only, guarded while gaming" principle: the plugin never restarts Steam on its own; the user does, when
  convenient.
- **Minimal new wire surface.** The advisories ride an added field on the existing `sync_preview` response
  (`pause_likely`, plus `sync_platform_count` / `sync_collection_count` for the preview scope line) and added fields on
  the existing `sync_complete` payload (`interrupt_reason`, `restart_recommended`). The only new backend name is the
  `get_session_budget_status` callable (which returns `rss_kb` + the fixed threshold lines `warn_kb`/`ceiling_kb`/
  `cliff_kb` + the retained `memory_delta_kb` + the live `resume_ready`); the "free memory" action is a pure frontend
  `SteamClient.User.StartRestart` call, so it adds no callable. No new emit event; the callable-manifest and
  event-parity gates stay green.
- **Parameterized for reclaimable API artwork (PR 2).** The gate's per-item cost is a parameter defaulting to the
  worst-case create rate. This PR does **not** reintroduce API artwork; the parameter is the seam PR 2 uses to add a
  per-item cover term once `SetCustomArtworkForApp` (transiently resident, GC-reclaimable — hence the GC-before-measure)
  returns.

## Consequences

- **A large first import becomes a planned, consent-based multi-stage process** instead of a per-boot crash lottery: it
  applies as far as the budget allows, pauses cleanly, and the user restarts Steam (resetting the budget) and resumes.
- **A resume can never drive itself over the cliff.** Making the first chunk _predictive against the cliff_ closes the
  earlier hole where the absolute-vs-ceiling first-chunk check let a resume at up to `cliff − margin` (≈2.2 GB) emit one
  unchecked chunk whose worst-case peak (≈2.2 + 0.3 = 2.5 GB) landed _above_ the crash floor — the ~50 MB window that
  made ADR-0023's "a resume can never cross the cliff" only almost true. The first chunk now projects its real cost
  against the cliff, so it proceeds only when that projected peak stays below the crash line and re-pauses otherwise:
  forward progress is guaranteed only when there is real headroom, exactly when it is safe. The claim is now literally
  true.
- **A single oversized platform re-touches its chunks across restarts (no crash).** A resume redoes an interrupted
  platform's chunks from the start (platform-level skip only), so a platform whose own apply exceeds one session's
  budget is applied in stages across Steam restarts, re-touching the already-applied chunks each time. It never crashes
  — the predictive gate stops each stage before the cliff — but the re-touch is wasted work until the delta-restricted
  apply tracked in #1383 lands. Measured budgets (~2000+ items/boot) exceed realistic single-platform sizes, so this is
  a rare edge PR 1 accepts.
- **~5 s of GC overhead per chunk boundary — only near the ceiling.** A small sync whose RSS stays below the 1.5 GB
  GC-skip floor pays zero GC cost (the raw reading already clears every threshold); only a run genuinely approaching the
  cliff pays the ~5 s settle, negligible against a chunk's ~2-minute apply, and it buys a truer reading than the
  unreliable natural GC.
- **Every path is fail-open.** No `steamwebhelper` process, unreadable `/proc`, or an unreachable debugger yields a
  skipped gate (logged once per run) — the sync proceeds exactly as it did before this decision. Measurement is a safety
  net, never a dependency.

## Alternatives considered

- **Count-based thresholds** ("pause after N shortcuts"). Rejected: the per-boot create-rate variance makes any count
  threshold wrong in both directions — too eager on a cheap boot, too late on an expensive one. RSS is the ground truth;
  counts are a lossy proxy for it.
- **A continuous RSS-watcher background task.** Rejected: the chunk boundary is the only point the run can stop cleanly
  and durably, so a watcher would only ever act there anyway — it adds a concurrency surface (racing the apply's shared
  state) for no earlier decision.
- **Forced restart when near the cliff.** Rejected as an _automatic_ action: it kicks the user out of Big Picture and
  breaks the QAM mid-interaction. Pausing keeps consent with the user. (The user _may_ restart on demand via the Restart
  Steam now button — consent-based, not forced, and disabled while a game is running.)
- **`Page.reload` on the SharedJSContext renderer for the "free memory" button.** Tried and rejected: on-device
  (2026-07-12) the reload was non-deterministic — Steam sometimes rebuilds the whole page family and the OLD renderer
  generation lingers (a ~2.2 GB process hosting the previous `uid2` page generation), so total footprint went UP and the
  UI stayed sluggish. A `Runtime.evaluate` of `SteamClient.Browser.RestartJSContext()` was also ruled out (CDP reports
  "Cannot find default execution context" on that target even after `Runtime.enable`). The button now does a
  deterministic full Steam client restart via `SteamClient.User.StartRestart` from the frontend instead — the renderer's
  per-session budget resets cleanly because the whole client restarts.
- **Relying on natural GC to keep RSS down.** Rejected: measured unreliable (minutes, or absent for 12+ minutes). An
  explicit GC before each reading is what makes the measurement trustworthy.
- **Raising the ceiling via an out-of-CEF bulk import** (`shortcuts.vdf` / an out-of-process importer). Deliberately out
  of scope, as in ADR-0023 — it requires Steam restarted or not running and is a separate, research-gated design. This
  decision hardens the in-CEF path we have.

## See also

- [#1383](https://github.com/danielcopper/decky-romm-sync/issues/1383) (session budget: measure, warn, pause)
- [ADR-0023](0023-chunked-per-unit-apply.md) (chunked apply — the resume mechanics this pause reuses, and the
  operational envelope it first documented)
- [Backend Architecture](../architecture/backend-architecture.md) (the apply pipeline and the session-budget seams)
- [Steam Non-Steam Shortcuts](../architecture/steam-non-steam-shortcuts.md) (why each shortcut costs renderer memory)
