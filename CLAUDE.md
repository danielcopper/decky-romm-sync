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
- **Frontend↔backend callable parity (names + arity)** — check — `scripts/check_callable_manifest.py`
- **Every backend `emit` event name has a frontend listener, and vice versa** — check — `scripts/check_event_parity.py`
- **`settings.json` is written only by its owner (`adapters/persistence.py`)** — check —
  `scripts/check_settings_owner.py`
- **Sync run-lifecycle (`sync_state` / `current_sync_id`) written only via `LibrarySyncStateBox` verbs** — check —
  `scripts/check_sync_lifecycle_owner.py`
- **Aggregate state mutated only via verb-named methods (no field assignment)** — check —
  `scripts/check_aggregate_field_assignment.py`
- **No UoW-opening seam (ActiveCoreResolver, RelaunchOptionsResolver, uow_factory) is called while a UoW is open on the
  same path** — check — `scripts/check_uow_seam_nesting.py`
- **Services never call clocks / sleep / uuid / random directly (inject the Protocol)** — check —
  `scripts/check_cosmic_call_bans.sh`
- **No module in `services/` or `bootstrap/` crosses the ~700-LOC decomposition threshold, and the ones already over it
  may not grow** — check — `scripts/check_module_size.py` (the modules that predate the gate are grandfathered at their
  exact size; that list only ever gets shorter)
- **Service-independence contract list stays complete** — check — `scripts/check_service_independence_contract.py`
- **Layer import direction (services ↛ adapters, adapters ↛ services, …)** — check — `.importlinter` (`lint-imports`)
- **No bare `# type: ignore` / blanket suppressions** — check — `scripts/check_no_bare_ignores.sh`
- **Every pinned version in `requirements-*.lock` satisfies its `requirements-*.txt` source constraint** — check —
  `scripts/check_lock_sync.py`
- **Every local markdown link in tracked docs resolves (file target + heading/attr-list anchor)** — check —
  `scripts/check_markdown_links.py`
- **Every stated RomM minimum version matches the enforced `Plugin._MIN_REQUIRED_VERSION`** — check —
  `scripts/check_romm_min_version.py` (ADRs excluded: frozen history)
- **Server-supplied path components pass `safe_join` (`lib/path_safety.py`)** — test + prompt-only — traversal tests per
  path builder; new call sites are prompt-only
- **No sentinel objects on the wire — explicit JSON-representable tagged values only** — prompt-only — mechanizable once
  tagged values have replaced the sentinels
- **Every destructive op has backup-or-confirm; never delete data that exists nowhere else** — prompt-only — save-file
  removals route through `MatrixExecutor.quarantine_local_file` (the `.romm-backup` funnel); every other delete path
  carries the rule unmechanized
- **Every read-mutate-write of a `RomSaveSyncState` runs under `SyncEngine.rom_lock(rom_id)`** — prompt-only — sync
  paths, `get_save_status`, and the four slot mutations hold the lock; mechanize via a `rom_save_sync_states.save`
  call-site audit
- **Per-slot server reads/deletes go through `domain/save_slot.py` (legacy omits `&slot=`, client-filters)** —
  prompt-only — `get_slot_saves` / `get_slot_delete_info` / `delete_slot` / `list_file_versions` / `rollback_to_version`
  use `slot_query_param` + `save_in_slot`; RomM can't address `slot:null` via the param, so legacy MUST omit it + filter
  client-side, and a legacy delete is refused up-front
- **Every save-sync decision comes from `compute_sync_action` (via `list_saves`), never the `negotiate` op list; every
  automatic upload POSTs `overwrite=false` (409-backstopped); `overwrite=true` only from an explicit `keep_local`** —
  test + prompt-only — domain property tests (`resolve_upload_conflict`, row-11 split, Inv7/Inv8) + contract 409 tests
  (`tests/contract/`); new upload/dispatch call sites are prompt-only
- **`applied_launch_options` is written only by the five recorded-state writer sites (sync ack-commit,
  download-complete, uninstall, home-migration, version-switch), each recording the exact command the frontend wrote;
  excluded from the sync UPSERT; the only sanctioned reset is Force Full Sync's clear-to-NULL (a wrong recorded value is
  the only path to a wrong delta-skip)** — test + prompt-only — each writer site carries a value-exact test; new
  launch-options write paths are prompt-only — mechanize via a `set_applied_launch_options` /
  `record_applied_launch_options` call-site audit
- **An abandoned-chunk stash's whole-unit apply staging (`pending_sync` / `pending_all_roms` / `pending_cover_sources`)
  is never mutated while the stash is pending (box IDLE) — every run-entry path passes `try_begin_run`, which clears the
  stash before any staging write** — prompt-only — the invariant holds today rather than being aspirational; mechanize
  via a staging-writer call-site audit

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
