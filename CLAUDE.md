# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via
RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## What belongs in this file

Three things, and nothing else:

1. **Traps** — where an agent would confidently do the wrong thing and have no reason to go look first.
2. **Cross-cutting invariants** — rules that span files, so no single diff shows the whole rule.
3. **Workflow** — how we work here; not derivable from the code.

Everything else is topic depth: `docs/` for humans, `.claude/rules/` for path-scoped coding conventions. A rule with a
mechanical check needs only its one-line statement here (CI carries the enforcement); a rule without one needs its full
statement here or in the rule file that owns its area, because nothing else will catch it.

## Where the details live

Each page below is the current-truth owner of its area, and most carry their own ADR trail. Read the page before working
in the area. **Do not cite ADRs from this file** — an ADR is frozen history (and may be `Proposed` or superseded, which
is invisible at the citation site), so reach it through the page that owns the topic.

- Domain vocabulary — the canonical meaning of project terms — [CONTEXT.md](CONTEXT.md). A glossary, not a spec: use its
  wording in code, issues, and PRs, and add a term there the moment it resolves in discussion.
- Steam shortcuts — appIds, artwork, launch-option writes, removal churn —
  [steam-non-steam-shortcuts.md](docs/architecture/steam-non-steam-shortcuts.md)
- Save-file sync — slots, conflict resolution, negotiate transport, version history —
  [save-file-sync-architecture.md](docs/architecture/save-file-sync-architecture.md)
- Save-sync coverage matrix — [save-sync-coverage.md](docs/architecture/save-sync-coverage.md)
- Removed-game cleanup — deletion authority, admission/leases, claims, recovery bundles —
  [removed-game-cleanup.md](docs/architecture/removed-game-cleanup.md)
- Services, adapters, wiring; connection/token and settings-persistence internals —
  [backend-architecture.md](docs/architecture/backend-architecture.md)
- SQLite schema, aggregate roots, migrations — [database-design.md](docs/architecture/database-design.md)
- Emulator and core selection — [core-emulator-selection.md](docs/architecture/core-emulator-selection.md)
- RetroArch/ES-DE config parsing — [config-source-parsers.md](docs/architecture/config-source-parsers.md)
- Steam Remote Play — [steam-remote-play.md](docs/architecture/steam-remote-play.md)
- **End-user-facing behavior and UI** — setup, configuration, syncing, save-sync, BIOS, troubleshooting —
  `docs/user-guide/`
- Dev setup, dependency management, frontend loop — `docs/contributing/`

## Path-scoped rules — `.claude/rules/`

Coding conventions live in `.claude/rules/*.md`, each carrying a `paths:` frontmatter glob. They are plain Markdown, not
a harness-specific format: an agent that supports path-scoped rules gets one loaded when a matching file is read, and
**every other agent must open the file itself**. Either way it arrives a beat late for code you are **creating** rather
than editing, so the entries below lead with what goes wrong unnoticed. Read the rule that owns an area before writing
new code in it.

- `services.md` — a new service takes **one `config: XxxServiceConfig` kwarg** (frozen, all deps inside); debug logging
  is the injected `DebugLogger`. Neither is checked.
- `python-conventions.md` — Protocol suffixes by shape, `do_<verb>` vs. `_<verb>_io`, docstrings stating the contract
  rather than the behavior, and when a subfolder is justified. **No mechanical check exists for any of it.**
- `adapters-domain.md` — adapters own I/O, domain is pure, aggregate mutations are verb-named after the event
  (`adopt_baseline`, not `update_baseline`). The field-assignment ban is checked; the naming is not.
- `romm-http.md` — an unproven 404 must never become `RommNotFoundError`, which is deletion authority downstream: the
  entity proof is the default and only the three byte-stream fetches opt out. Tests pin both directions; nothing else
  does. Also owns the transport's reachability state: a new request method sends through `_urlopen` (checked) and passes
  `romm_origin=False` if it does not talk to RomM (not checked).
- `bootstrap-wiring.md` — the `main.py` / `bootstrap/` split, and which half of `bootstrap/` new wiring belongs in.
- `callables.md` — the `{success, reason, message}` failure shape and its two carve-outs. Checked.
- `vendored-assets.md` — `_vendor/`, `native/`, `defaults/` are checksum-pinned verbatim copies. The checksums are
  checked; the reflex to fix the upstream artifact instead of the copy is not.
- `testing-backend.md` — test tiers, gate tests, vendored conformance vectors.
- `testing-frontend.md` — the `@decky/api` event harness, non-vacuous catch assertions.
- `comments.md` — an inline comment is the exception: only an outside-world fact, a road not taken, or a constraint the
  code cannot express. Re-read the comment on the line you touch — a stale one is worse than none, because it is
  believed and nothing in the toolchain contradicts it. **No mechanical check exists.**

## Documentation

**Docs are updated in the same PR as the code change. This is not optional.** When a change affects architecture, data
flows, feature behavior, or user-facing UI, the relevant page under `docs/` must be updated in the same PR.
Documentation-debt-as-a-separate-follow-up-issue is forbidden — those follow-ups never land. If you're not sure whether
a change needs docs, the default is "yes, it does." Enforced in CI by `.github/workflows/docs-check.yml`.

For genuinely doc-irrelevant PRs (pure refactor with no user-visible change, no architecture shift, no new flow;
tooling/CI changes; dependency bumps), set the `no-docs-change` label on the PR OR include `docs: N/A` (with a one-line
reason) in the PR description. Opting out is an explicit acknowledgement, not a silent omission.

Docs are Material for MkDocs, published to GitHub Pages by `.github/workflows/docs.yml` on push to `main`. Preview
locally with `mise run docs`.

## Traps — non-obvious rules that bite silently

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart;
  SteamClient API is instant.
- **AddShortcut ignores most params**: `AddShortcut(name, exe, startDir, launchOptions)` ignores startDir and
  launchOptions. Must use `Set*` calls (`SetShortcutName`, `SetShortcutExe`, `SetShortcutStartDir`,
  `SetAppLaunchOptions`) once the new app's overview is registered. Do NOT pass quoted exe paths — the API quotes
  internally.
- **AddShortcut timing**: After `AddShortcut()`, wait for the new app's overview before setting properties — poll
  `appStore.GetAppOverviewByAppID(appId)` (`waitForAppOverview`), never a blind fixed delay. Use 50ms between operations
  in the apply loop.
- **Shortcut appId is assigned, not derived**: Steam assigns it at creation and it is stable for the shortcut's
  lifetime; the plugin records it in `roms.shortcut_app_id` and detects ownership by the exe path. Never re-derive it
  (the `CRC32(exe + appName)` formula is disproven). `launchOptions`/`startDir` changes are appId-safe; **exe/name**
  changes require delete + recreate.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT
  `ServerAPI.callPluginMethod()`).
- **Decky callables must be async**: Even if the body is synchronous, Decky's callable framework requires `async def`.
  Do not remove `async` from callable methods in `main.py`.
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode).
  Paginated: `{"items": [...], "total": N}`. List calls page via `lib/romm_paging.py` and append
  `&with_char_index=false&with_filter_values=false` to skip aggregations the server otherwise computes on every request.
- **RomM minimum version**: Requires RomM >= 4.9.0, hard-rejected in `test_connection()` (`_MIN_REQUIRED_VERSION` in
  `main.py`) — the plugin is inert until the server is updated.
- **User-Agent on outgoing HTTP**: SteamGridDB **and** RomM behind Cloudflare Tunnel reject the default `Python-urllib`
  UA with 403. Every HTTP-talking adapter takes a `user_agent: str` ctor param; bootstrap threads
  `decky-romm-sync/<version>` from `package.json` — no hardcoded version strings.
- **Large payloads**: Never send bulk base64 through `decky.emit()` — the WebSocket bridge has size limits. Use per-item
  callables, and chunk bulk lists (the library apply emits shortcuts in batches; the metadata cache loads page-by-page).
- **No `BIsModOrShortcut` bypass**: the bypass counter was removed deliberately. Shortcuts return `true` (natural
  state); we own the game detail UI. Do not reintroduce a bypass.

## Current State

Latest release and shipped features: see `git tag --sort=-v:refname` and GitHub Releases. Roadmap and open work:
[GitHub Projects board](https://github.com/users/danielcopper/projects/2).

## Development

- **Build**: `pnpm build` (Rollup -> dist/index.js)
- **Tests**: backend — `python -m pytest tests/ -q` or `mise run test`; frontend — `mise run test:frontend` (Vitest +
  happy-dom)
- **Coverage**: backend — `python -m pytest tests/ -q --cov=py_modules --cov=main --cov-report=term --cov-branch`;
  frontend — `mise run test:frontend:coverage`
- **Lint**: `mise run lint` (import-linter, the `scripts/check_*` gates, markdownlint). Ruff and basedpyright run only
  inside `mise run gate`.
- **Gate**: `mise run gate` (the full CI battery in one command — mirrors every PR check; slow. Run before pushing.)
- **Setup**: `mise run setup` (installs JS + Python dependencies)
- **Dev reload**: `mise run dev [display]` (build + restart plugin_loader; a display like `dp4` / `internal` also opens
  windowed BPM on it after the deploy)
- **Frontend live dev**: `mise run dev:watch [display]` (one-time `mise run dev:setup`) — hot-reloads the **frontend**
  into windowed Big Picture on every save, no loader restart. **Backend** changes need `mise run dev:push-backend`. Lost
  the Decky UI after leaving BPM: `mise run dev:bpm-reset [display]`. Guide: `docs/contributing/frontend-dev-loop.md`
- **Tooling**: mise manages node, pnpm, python, uv; venv auto-creates at `.venv`. Python deps are pinned in
  `requirements-*.lock`, compiled from `requirements-*.txt` by `uv pip compile`; regenerate with `mise run lock-update`
  after editing a source or bumping a pin.
- **Pre-commit hook** (`.githooks/pre-commit`): formats staged files — `ruff format` + `ruff check` (Python),
  `prettier --write` (TS/TSX), `deno fmt` (Markdown). Stays fast (<2s); heavy validation is CI-only. Do not re-introduce
  heavy checks here.

## Code Quality

CI runs SonarCloud (Quality Gate: 80% coverage on new code, 0 bugs, 0 vulnerabilities), Ruff, basedpyright,
import-linter, pytest-cov branch coverage, and the repo's `scripts/check_*` gates. **The per-rule checks and what each
one enforces are listed in the invariant register below** — that table is the single inventory; do not duplicate it
here.

## Invariant register — cross-cutting safety rules

The audit's clearest pattern: every rule with a mechanical check held; every rule that lived in prose or in a reviewer's
head drifted. This register is the single inventory of the cross-cutting safety rules — the ones that span files, so no
diff-scoped review (human or agent) sees the whole rule — plus the current enforcement tier of each. It is a **map of
the enforcement surface, not the enforcement itself**: a `check`/`test` rule is enforced by the named artifact; a
`prompt-only` rule is not yet mechanized and is injected here so review carries it verbatim until a check exists. The
moment a `prompt-only` rule gets a mechanical check it moves to the `check` tier — a rule is never weakened to stay
green, and a real drift is a finding to triage, never an exemption. `[ours]`

Format: **invariant** — tier — enforced by.

- **Callable failures use `{success, reason, message}` (never `error` / `error_code`)** — check —
  `scripts/check_failure_shape.py --check`
- **A definitive 404 is `not_found`, never `server_unreachable` — a catch-all `except Exception` in `services/` may not
  bind a verdict key (`reason` / `status` / `recommended_action`) to a hardcoded `SERVER_UNREACHABLE`; route the
  exception through `classify_error`, or peel the 404 off with a sibling `except RommNotFoundError` where the verdict is
  a partial-success flag** — check — `scripts/check_404_not_unreachable.py --check`
- **A 404 becomes `RommNotFoundError` only when RomM's entity layer is proven to have answered it; only the three
  byte-stream fetches opt out** — test + prompt-only — `TestNotFoundDiscrimination` plus the per-call-site
  `test_generic_route_404_still_raises_not_found` trio in `tests/adapters/romm/test_http.py`; a fourth byte-stream
  fetch's opt-out is prompt-only — `.claude/rules/romm-http.md`
- **Every RomM request goes out through `RommHttpAdapter._urlopen`, the single point that clears the known-unreachable
  state — a request method calling `urllib.request.urlopen` itself leaves every retry ladder degraded to one attempt
  until an unrelated path happens to succeed, and nothing else fails** — check — `scripts/check_urlopen_choke_point.py`
  (structural, AST call sites — an alias or a `getattr` would slip past it. Which requests may skip the ladder, and
  which pass `romm_origin=False` because they do not talk to RomM at all, stays prompt-only in
  `.claude/rules/romm-http.md`)
- **Frontend↔backend callable parity (names + arity)** — check — `scripts/check_callable_manifest.py`
- **Every backend `emit` event name has a frontend listener, and vice versa** — check — `scripts/check_event_parity.py`
- **`settings.json` is written only by its owner (`adapters/persistence.py`)** — check —
  `scripts/check_settings_owner.py`
- **Sync run-lifecycle (`sync_state` / `current_sync_id`) written only via `LibrarySyncStateBox` verbs** — check —
  `scripts/check_sync_lifecycle_owner.py`
- **A library-sync seam is held only by the module owning the job it belongs to: `active_core` / `disc_resolver` by
  `services/library/bake_inputs.py`, `renderer_rss` / `renderer_gc` by `services/library/session_budget.py`. The
  `service.py` façade is not a co-owner — it may **pass** a seam on and may not **use** one, which the check reads
  structurally: a seam attribute standing as a call's keyword-argument value, and a seam annotation on a field of
  `LibraryServiceConfig`, are wiring; anything else in the façade is a finding** — check — `scripts/check_seam_owner.py`
  (AST over attributes named after a seam and over annotations naming its Protocol, resolved per seam rather than per
  module — owning one grants nothing about another. It sees the injection, not every later use: a seam aliased to a
  differently-named attribute or local, one reached through `getattr`, one passed positionally into a helper that holds
  it, a quoted string annotation, and a Protocol imported under an alias all slip past it — the last two only on the
  annotation half, because the constructor read that unpacks the config is flagged whatever the field is called). What
  the confinement buys is that each module's transactions stay separable: `bake_inputs` holds a read UoW across its
  install-path scan while the `active_core` seam opens its own `BEGIN IMMEDIATE` per ROM, so folding the two into one
  pass deadlocks — on a real device only, since `FakeUnitOfWork` shares no connection. **Nothing mechanical stands
  behind that half.** `check_uow_seam_nesting.py` catches the fold only in its inline form (the seam method named inside
  the `with` block); the peer-call form — `do_build_core_overrides` invoked from inside the install-path readers' own
  UoW, which is what putting the three methods on one class makes cheapest — is that gate's documented blind spot and
  passes green. On the budget side the confinement holds `session_budget`'s stated promise that no renderer-RSS reading
  is taken anywhere else in the package
- **A module declared read-only calls no repository write — `services/library/local_library_reader.py` to start** —
  check — `scripts/check_read_only_module.py` (AST over the declared file's own calls, matching the two-attribute
  `<...>.<repo>.<method>` shape against the eleven repositories the UoW exposes). Read or write is decided **by the
  name's shape** — `get` / `get_*` / `iter_*` / `count` plus the explicitly listed reads — and
  `tests/scripts/test_check_read_only_module.py` re-derives every method name from `services/protocols/repositories.py`
  and pins its classification, so a new repository method fails there until it is classified. **The two directions are
  not symmetric.** A read named outside the shapes is called a write: loud, safe. A **write named like a read is called
  a read and passes in silence** — `uow.roms.get_or_create(...)` is green today, which is the very accident class this
  entry is otherwise about, so "repository writes are named as writes" stays a prose rule the gate does not carry. It
  also sees only calls the file itself makes, and only as calls: a write behind a helper it calls, a write **passed as a
  bound method** (`run_in_executor(None, uow.roms.save, rom)` — an attribute, not a call, and the exact idiom this
  module's own reads are invoked through), an aliased handle (`repo = uow.roms`), a `getattr`-reached repository, and a
  repository name not in its list all pass green. What it does catch is the plain `uow.roms.save(...)` dropped into a
  read module because a UoW was already open there. The declaration earns its keep by making the boundary checkable
  rather than descriptive: these reads open their own short UoW and are offloaded to an executor at points chosen for
  cheapness, so a write among them would land at a moment nobody picked. The platform stamp's DELETE stayed in
  `sync_orchestrator.py` on those grounds — it is a write, so a read-only module cannot hold it, and it is cohesive with
  the apply pipeline that performs it, not with the projections a run reasons about. **Why it sits exactly where it sits
  in that pipeline is a property of its call site, not of its module** — after the fetch, after the artwork, after the
  cancel guard, before the first chunk (ADR-0023 / #1025) — and that argument lives in full at the call site's own
  comment, which is the only place a move could not have carried it away from
- **An emitted `sync_progress` frame stops a run (`running: False`) only with a terminal stage, and a terminal stage is
  only ever emitted with the run stopped** — test — `tests/services/library/test_terminal_frame_contract.py`
  (structural, AST call sites and dict literals across all of `py_modules/services` — it covers the error paths a
  behavioural test would have to provoke one at a time, but a frame assembled by a helper it cannot follow, or emitted
  through an aliased callable, slips past it; its own scope tests pin the producers and the root it reaches, so a
  narrowing fails rather than shrinking the rule in silence. Frame producers are not confined to `services/library/`:
  `services/artwork.py` emits through an injected `emit_progress`). The QAM panel derives "a run is in flight" from
  `running` and keys the run's end — the status line, the live-ETA teardown, the two change-driven re-reads — on the
  stage, so a stopping frame with a non-terminal stage would collapse the in-progress rows while ending nothing. The
  panel cannot defend against it: a bare `running: false` is exactly what its own retraction of an optimistic start
  looks like
- **Aggregate state mutated only via verb-named methods (no field assignment)** — check —
  `scripts/check_aggregate_field_assignment.py`
- **No UoW-opening seam (ActiveCoreResolver, RelaunchOptionsResolver, uow_factory) is called while a UoW is open on the
  same path** — check — `scripts/check_uow_seam_nesting.py`
- **Services never call clocks / sleep / uuid / random directly (inject the Protocol)** — check —
  `scripts/check_cosmic_call_bans.sh`
- **No module in `services/`, `bootstrap/`, `adapters/`, `domain/`, `lib/` or `models/` crosses the ~1000-LOC
  decomposition threshold, and the ones already over it may not grow** — check — `scripts/check_module_size.py` (the
  modules that predate the gate are grandfathered at their exact size; that list only ever gets shorter. `main.py`,
  `_vendor/`, `tests/`, `scripts/` and `src/` are out of scope, each for a reason recorded at `SCOPE_DIRS`)
- **Service-independence contract list stays complete** — check — `scripts/check_service_independence_contract.py`
- **Layer import direction (services ↛ adapters, adapters ↛ services, …)** — check — `.importlinter` (`lint-imports`)
- **Frontend direction: `src/utils/` and `src/api/` never import `src/components/`, and no `src/` module takes part in
  an import cycle** — check — `eslint.config.js` (`import-x/no-restricted-paths`, `import-x/no-cycle`). These rules go
  inert rather than loud when misconfigured: `import-x/extensions` ships as `['.js']`, so until it names `.ts`/`.tsx`
  the plugin resolves an import but never opens the target to read its imports, and `no-cycle` reports nothing on any
  codebase. `src/eslintBoundaries.test.ts` lints known-bad fixtures through the real config and fails if any of the
  three stops reporting — a green `pnpm lint` alone proves nothing. Type-only imports are not edges (erased at runtime),
  which is why the `api/backend.ts` ⇄ `utils/cachedGameDetailStore.ts` back-reference is not a cycle
- **No bare `# type: ignore` / blanket suppressions** — check — `scripts/check_no_bare_ignores.sh`
- **Every pinned version in `requirements-*.lock` satisfies its `requirements-*.txt` source constraint** — check —
  `scripts/check_lock_sync.py`
- **Every local markdown link in tracked docs resolves (file target + heading/attr-list anchor)** — check —
  `scripts/check_markdown_links.py`
- **Every stated RomM minimum version matches the enforced `Plugin._MIN_REQUIRED_VERSION`** — check —
  `scripts/check_romm_min_version.py` (ADRs excluded: frozen history)
- **Server-supplied path components pass `safe_join` (`lib/path_safety.py`)** — test + prompt-only — traversal tests per
  path builder; new call sites are prompt-only
- **No sentinel objects on the wire — explicit JSON-representable tagged values only** — prompt-only — no sentinel
  survives on the wire today (`NO_MIGRATION` retired with #1004, legacy `slot:null` confirmation with #1276), so the
  rule now guards reintroduction; nothing mechanical detects a new one
- **Every destructive op has backup-or-confirm; never delete data that exists nowhere else** — prompt-only — save-file
  removals route through the `.romm-backup` funnel (`MatrixExecutor.quarantine_local_file`; the removed-game cleanup's
  claimed variant is `PruneSaveSupport.quarantine_prune_saves`); every other delete path carries the rule unmechanized.
  Removed-game cleanup takes the **confirm** leg for one case deliberately: installed ROM content the user did not
  select for the recovery bundle is deleted with its row. The ROM is re-downloadable from RomM where a save is not, the
  per-candidate opt-in and its consequence are stated in the confirmation dialog and the user guide, and the row cannot
  be removed at all without a fresh 404 — so this is a disclosed choice, not an exception that drifted in. The adopt
  dialog's **replace** exit is the second such case, and it does **not** rest on that justification: the premise is that
  the content is the user's own — a different rip, a patch, a romhack — which is exactly what the server cannot hand
  back. What carries it instead is that the user is shown both sides, offered a content check, and chooses between two
  named outcomes behind a second confirmation ([ADR-0028](docs/adr/0028-adopted-install-is-an-install.md)). That
  reasoning covers the **ROM** only: an adoption's Overwrite also replaces save and savestate files, and those take the
  **backup** leg through the same `MatrixExecutor.quarantine_local_file` — every argument ADR-0028 gives for not
  quarantining a ROM (gigabytes, no sensible retention, re-fetchable from RomM) inverts for a save, and a savestate is
  synced nowhere at all. It is the first caller to hand that funnel a directory outside the saves root: it takes the
  directory it is given, so a savestate's backup lands in `<states>/.romm-backup/`
- **Every read-mutate-write of a `RomSaveSyncState` runs under `SyncEngine.rom_lock(rom_id)`** — prompt-only — sync
  paths, `get_save_status`, and the four slot mutations hold the lock; mechanize via a `rom_save_sync_states.save`
  call-site audit
- **Per-slot server reads/deletes go through `domain/save_slot.py` (legacy omits `&slot=`, client-filters)** —
  prompt-only — `get_slot_saves` / `get_slot_delete_info` / `delete_slot` / `list_file_versions` / `rollback_to_version`
  use `slot_query_param` + `save_in_slot`; RomM can't address `slot:null` via the param, so legacy MUST omit it + filter
  client-side, and a legacy delete is refused up-front
- **Every save-sync decision comes from `compute_sync_action` (via `list_saves`), never the `negotiate` op list; every
  automatic upload POSTs `overwrite=false` (409-backstopped); `overwrite=true` only from an explicit `keep_local`** —
  test + prompt-only — the hand-enumerated core cases (`tests/adapters/test_gavel_native_decision_table.py`) and the
  core property tier (`tests/adapters/test_gavel_native_property.py`) + contract 409 tests (`tests/contract/`); new
  upload/dispatch call sites are prompt-only
- **Both save-sync decisions run in the compiled gavel core, reached only through the `ComputeSyncActionFn` /
  `ResolveUploadConflictFn` seams; `domain/sync_action.py` holds only the `SyncAction` vocabulary the core answers in,
  so a change to either decision is a contract change carried by re-copied gavel vectors** — test — both vendored vector
  families run against the core (ladder in `tests/adapters/test_gavel_native.py`, decision table in
  `tests/adapters/test_gavel_native_table_vectors.py`); the `.so` and the vectors are pinned to the same upstream
  release tag and are bumped together
- **`applied_launch_options` is written only by the six recorded-state writer sites (sync ack-commit, download-complete,
  adopt-complete, uninstall, home-migration, version-switch), each recording the exact command the frontend wrote;
  excluded from the sync UPSERT; the only sanctioned reset is Force Full Sync's clear-to-NULL (a wrong recorded value is
  the only path to a wrong delta-skip)** — test + prompt-only — each writer site carries a value-exact test; new
  launch-options write paths are prompt-only — mechanize via a `set_applied_launch_options` /
  `record_applied_launch_options` call-site audit. Download-complete and adopt-complete are one site in the code
  (`RomInstallRecorder.do_record_applied_launch_options`) and two in the flow, because an adopted install is an install
  in every respect (ADR-0028)
- **An abandoned-chunk stash's whole-unit apply staging (`pending_sync` / `pending_all_roms` / `pending_cover_sources`)
  is never mutated while the stash is pending (box IDLE) — every run-entry path passes `try_begin_run`, which clears the
  stash before any staging write** — prompt-only — the invariant holds today rather than being aspirational; mechanize
  via a staging-writer call-site audit
- **A prune run's claim reservation and its refusal of every conflicting callable happen in one atomic gate hold (the
  preview rebuild does not), and frontend-owned Steam work holds a heartbeated, generation-tombstoned lease through
  every continuation's final write** — test + prompt-only — prune service/gate race tests + contract callable-entry
  matrix; new conflicting entry points are prompt-only
- **A prune frontend action mutates Steam only after atomically claiming its exact run/token/discriminant/binding;
  repeats are idempotent and an outcome lost in transit is ambiguous, never success** — test + prompt-only — prune
  service claim tests + `src/utils/pruneActions.test.ts`; new action kinds are prompt-only
- **Every installed-content mutation is authorized by a descriptor-relative no-follow claim (root identity, descendant
  identities, and — where a bundle exists — regular-file hashes) revalidated immediately before it, never by a path
  re-lookup; refusal, partial mutation and ambiguity are reported, never rewritten into success. The hashes bind a
  deletion to bytes held somewhere else, so they follow the **bundle**, not the caller: a source a sealed bundle holds
  consumes the bundle's digest-bound claim, a source it did not capture seals a fresh content-bound one, and a removal
  with no bundle anywhere — recovery off, or a user-initiated uninstall — seals identity-only
  (`claim_source(..., digest=False)`). An identity-only claim is also the only one that may adopt interrupted
  `.{basename}.romm-prune-*` staging, and only where a surviving install row proves the path. Everything else holds for
  both: staging rename, mount checks, no-follow traversal, and exact-identity revalidation under writer exclusion held
  across each unlink. The one guarantee that differs is all-or-nothing: a content-bound removal leases the whole tree up
  front, an identity-only directory leases per unlink (a whole-tree hold hits `EMFILE` and makes large dumps
  un-uninstallable), so a writer arriving mid-loop yields a reported partial removal instead of a clean refusal** —
  test + prompt-only — descriptor-path, recovery-adapter, real RomRemovalService, and prune contract tests; new mutation
  adapters are prompt-only
- **Every prune frame carries its originating preview ID; only a matching pending preview may adopt a run, and an
  accepted contiguous terminal result seals it against every later frame** — test + prompt-only — prune service frame
  tests + `src/utils/pruneStore.test.ts`; new prune frame types are prompt-only
- **Every destructive RomM proof is bound to one canonical server-origin/token-origin/user namespace from preview
  through every exact-ID request; a namespace change is uncertainty, never a 404 deletion authority** — test +
  prompt-only — prune service namespace-race tests; new destructive RomM proof paths are prompt-only
- **Every write into per-rom detail state that crosses an `await` is bound to a rom identity — the store
  (`src/utils/gameDetailStore.ts`) via `writerForRom`, or the answer's own `rom_id` in `applySaveStatus`; the panel's
  state, event and tab-content modules (`src/components/panelState.ts`, `src/components/panelEvents.ts`,
  `src/components/panelTabContent.tsx` — the panel component itself holds none of these writes) via `RomBinding`, built
  by `bindRom` for a read a run of the `[appId]` effect issued and by `bindRomInState` for the active tab's panes, whose
  writer is built during render; the achievements tab (`src/components/AchievementsTab.tsx`) by construction, its React
  key being the rom id, so its state cannot outlive the identity it was read for. A version switch re-keys without
  closing, so neither the store's generation counter nor the panel's `[appId]` effect sees this class. Binding answers
  the wrong-rom question only; two answers for the SAME rom are ordered instead, by a sequence taken when the read is
  issued — the store's `loadSeq`, the panel's `takeReadTicket` (#1717). Four writes are unbound: the two identity writes
  install what a binding would compare against, so ordering is all they can have, and both have it. The other two have
  neither — the store's `cached.bios_status` fold runs in the same synchronous run as its guard, and the event lane's
  `handleBiosChange` answers for the platform's default core and can overwrite a rom-keyed answer (#1718). The panel's
  remaining lazy lane writes through the raw setter, ordered by the same ticket. The play button is NOT covered
  (#1714)** — test + prompt-only — the panel's thirteen bound sites each carry a version-switch test
  (`src/components/RomMGameInfoPanel.test.tsx`); the store side and every new write site on either are prompt-only,
  because a checker scoped to the store's own function bodies would be green on the case this rule was written for. The
  reasons behind the two writer mechanisms live at `writerForRom` and `RomBinding` — do not restate them here

When a change applies a guard / sanitize / backup / grouping pattern, sweep for sibling sites of the same pattern — the
register is what that sweep checks against.

## Security

- NEVER read or use credentials from settings files (`~/homebrew/settings/`) without explicit user permission
- NEVER pass credentials to agents — if API calls are needed, ask the user to run them and provide output
- NEVER log secrets (passwords, API keys) — mask them in any log output

## Working Style

- **Research before implementing.** When encountering an unknown (how a third-party tool works, where files are stored,
  what APIs exist), STOP and research first. Present findings and agree on an approach before implementation.
- **Discuss architecture decisions.** This is not a vibe coding project. Non-trivial changes require discussion before
  code is written. When you find a problem, explain it and propose options — don't just start fixing.
- **Use agents** for everything beyond trivial single-file edits — research, exploration, implementation. Keep main
  context on architecture and coordination.
- **Sequential agent discipline.** Each agent's prompt MUST include: "When done, report back and wait for shutdown. Do
  NOT pick up other tasks from the task list."
- **Preserve context.** Get alignment first, then implement cleanly in one pass.
- **Sub-issue policy**: epic bodies do **not** carry markdown sub-issue lists — open work is tracked via GitHub's native
  Sub-Issues panel. Link new sub-issues natively; don't add body bullets.
- Roadmap: [GitHub Projects board](https://github.com/users/danielcopper/projects/2).
