# Memory Index

Last consolidated: 2026-05-26.

## [appid-launchoptions-reliability.md](appid-launchoptions-reliability.md)

_Updated: 2026-06-03_ · type: project

Hardware-validated (#827, closed): `SetAppLaunchOptions` on an **existing** Steam shortcut IS reliable — in-session
(~150 ms read-back), across a full Steam restart, and over 30 add/set/remove churn cycles. appId = CRC32(exe + appname),
so `launch_options` / `startDir` mutations are appId-safe while `exe` / `name` are appId-destructive (orphan artwork /
playtime / collections). Narrows the overstated "shortcut property re-sync unreliable" framing in CLAUDE.md +
`steam-non-steam-shortcuts.md` (real hazard is a removal-churn corruption mode, not launch_options writes). Unblocks the
ADR-0005 reliable branch → #785 full refactor. Robust write pattern: fire `SetAppLaunchOptions` then poll `AppDetails`
to confirm. See also [[desktop-mode-test-constraints]], [[dev-deploy-loop]].

## [romm-injects-m3u-keep-es-de-gate.md](romm-injects-m3u-keep-es-de-gate.md)

_Updated: 2026-07-28_ · type: project

We are NOT the only producer of `.m3u` in a freshly extracted ROM dir — treat any playlist found there as foreign input.
The ES-DE `es_systems.xml` gate (`system_supports_m3u`, `adapters/es_de_config.py:259`) is load-bearing and must not be
narrowed to "only gate what we generate", even though `_maybe_generate_m3u_io` almost never fires against a real server.
Removing or narrowing it regresses #1111. Version-stamped upstream half (RomM 5.0.0, 2026-07-28): the server injects
`{fs_name}.m3u` into every multi-file download zip, ungated by platform — the original cause of #1111 — with three grep
paths to re-verify. See also [[romm-siblings-cannot-identify-discs]].

## [romm-siblings-cannot-identify-discs.md](romm-siblings-cannot-identify-discs.md)

_Updated: 2026-07-28_ · type: project

RomM has exactly ONE relation between related roms and it cannot tell "Disc 2 of the same release" from "the European
release". Never implement multi-disc grouping (#1554) on `sibling_group_key` alone — it silently merges regional
variants into one game with one save, and that mistake looks correct from inside our code because we already store the
key (#1295/#1297). Any grouping needs a disc discriminator the server does not supply; the fallback (declare the
per-disc library shape unsupported) is legitimate. Version-stamped upstream half (RomM 5.0.0, 2026-07-28) with
re-verification paths.

## [sync-ui-trigger-surfaces.md](sync-ui-trigger-surfaces.md)

_Updated: 2026-05-26_ · type: project

Which UI surface triggers which save-sync backend call, and which one actually surfaces `SyncConflictModal`. Three
surfaces: per-ROM Sync button (`sync_rom_saves` — toasts only), game-detail page open (`get_save_status` — refreshes
SAVES tab), Play button / `CustomPlayButton` (`pre_launch_sync` — opens the conflict modal in its "Resolve conflict"
state). Smoke-testing the conflict modal requires the Play button — the Sync button looks like the obvious entry point
but isn't.

## [save-conflict-test-path.md](save-conflict-test-path.md)

_Updated: 2026-05-26_ · type: project

How to produce a save conflict for testing. The newest-wins matrix in `domain/sync_action.py` has exactly one Conflict
path: `_decide_when_not_current`. Needs both sides diverged AND our device holding a (now-stale) sync entry on the
newest server save. Covers the `device_syncs` empty-without-`device_id` gotcha (re-verified 2026-05-20 on RomM 4.8.1,
NOT a regression), how a device gets its sync row (POST/download/confirm_download upsert; PUT does NOT on 4.8.1 →
confirm_download workaround, drop once min RomM ≥ 4.9.x per #748), and the no-device-authorship-column constraint
(`own_upload_ids` local-only; #276 records PUT uploads too). Related: #276 same area.

## [desktop-mode-test-constraints.md](desktop-mode-test-constraints.md)

_Updated: 2026-05-26_ · type: project

Steam Deck Game Mode vs Desktop Mode constraints. The Decky plugin toggle UI lives in Game Mode only; from Desktop the
user can stop/start `plugin_loader` via systemctl or switch sessions (the swap reloads the plugin). Game Mode and
Desktop Mode are temporally mutually exclusive — Claude runs in Desktop terminal, user exercises UI in Game Mode. No
live mid-UI injection — TOCTOU-style tests (inject server state while a modal is open) aren't doable; static setup →
switch → observe is the only viable shape. The `stale_conflict` / TOCTOU guard #384 is covered by service-layer unit
tests; on-device it's untestable here.

## [sonarcloud-findings-inline.md](sonarcloud-findings-inline.md)

_Updated: 2026-05-26_ · type: project

SonarCloud findings get surfaced inline in chat by the user, not via PR comments. SonarCloud runs on every PR + push to
main; Quality Gate enforces 80% coverage on new code, 0 bugs, 0 vulnerabilities (per CLAUDE.md). Expect findings
mid-session — address in the current commit when reasonable, otherwise note for a follow-up before the PR merges.

## [dev-deploy-loop.md](dev-deploy-loop.md)

_Updated: 2026-05-26_ · type: project

Standard tight dev loop on the Steam Deck: Claude edits in Desktop Mode (repo at `/home/deck/Repos/decky-romm-sync`) →
user runs `mise run dev` (builds via Rollup → `dist/index.js` and restarts `plugin_loader`, deploying locally) → user
switches to Game Mode (session swap reloads the plugin) → exercises UI on real hardware → reports observed behavior.
Real-hardware testing is fast and cheap — prefer asking the user to deploy and try a candidate fix over over-engineering
theoretical solutions. `mise run dev` is the canonical command; don't invent alternatives. See also
[[desktop-mode-test-constraints]].

## [release-build-sourcemap.md](release-build-sourcemap.md)

_Updated: 2026-05-26_ · type: project

Release build must strip source maps in `rollup.config.js`, NOT in a workflow step. `decky plugin build` rebuilds the
frontend itself (runs `rollup -c` in its build container) before zipping — workflow-level `rm -f dist/*.map` is dead
weight (this is how 0.18.0 shipped no asset, #763/#764). `@decky/rollup` hardcodes `output.sourcemap: true` and the
preset's options arg is silently reverted (`mergeAndConcat(options,
defaultOptions)` lets defaults win) — mutate the
returned object instead. Default `rollup.config.js` must be the map-free one; dev opts in via `rollup.dev.config.js`
(`pnpm build:dev` + mise `build` task). CI uses raw `pnpm build` → map-free.

## [release-zip-defaults.md](release-zip-defaults.md)

_Updated: 2026-05-26_ · type: project

What ships in the release zip vs the repo layout. `config.json` / `bios_registry.json` / `core_defaults.json` appear at
the ROOT of the release zip even though they live under `defaults/` in the repo — that's decky's `defaults/` convention:
contents are copied to the plugin root on install. Backend reads via dual path: root first (release), then `defaults/`
(dev, where `mise run deploy` rsyncs into a `defaults/` subdir). See `firmware.py`, `es_de_config.py`,
`adapters/romm/http.py`. Vendored `vdf` lib ships `vdf/__init__.py` + `vdf/vdict.py` only — `*.dist-info` removed (#764)
as unused pip metadata (`vdf` hardcodes `__version__`).

## [domain/cosmic-python.md](domain/cosmic-python.md)

_Updated: 2026-05-26_ · type: domain

Cosmic Python architecture rules for the backend (services / adapters / domain / lib / models). Forbidden in services:
concrete adapter imports (use Protocols from `services/protocols.py`), raw I/O (`os.*` beyond pure path algebra, `open`,
`pathlib` read/write, `fcntl`, `urllib`, `shutil`, `subprocess`), hidden I/O (`time.time`, `datetime.now`, `uuid.uuid4`,
`asyncio.sleep`, `random` — inject `Clock` / `UuidGen` / `Sleeper` Protocols per #294), service-to-service concrete
imports, module-function imports from `domain/`. God-class signal: services > 600 LOC or `__init__`

> 6 params (S107). Smell → fix mapping table. Refactor wave plan link (#277): Wave 1 (#256) → Wave 2 (#295) → Wave 3
> (#297–#302) → Wave 4 (#274 + #277 verify), with saves vertical (#254) in parallel.
