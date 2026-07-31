---
name: romm-injects-m3u-keep-es-de-gate
description: We are NOT the only producer of .m3u in our extract dir — the RomM server injects one into multi-file download zips, ungated by platform. The ES-DE es_systems gate in the download path is what stops that reaching systems whose emulator breaks on a playlist (#1111), so it is load-bearing even though our own `_maybe_generate_m3u_io` almost never fires in practice. Upstream half is version-stamped and must be re-verified.
type: project
---

# `.m3u` in the extract dir can come from the server — keep the ES-DE gate

**Durable rule (holds regardless of what upstream does):** the plugin is **not** the only producer of `.m3u` files
landing in a freshly extracted ROM directory. Treat any playlist found there as **foreign input**, and keep the
per-system gate that decides whether a playlist is allowed to be the launch target at all.

**Why:** `_maybe_generate_m3u_io` (`services/downloads.py`) reads like the only m3u source in the system, and it skips
whenever a playlist already exists — so against a real server it almost never fires. That makes it look like dead code
worth simplifying away, and makes the ES-DE gate look redundant ("we're the only producer, why gate our own output?").
Both readings are wrong and both regress [#1111](https://github.com/danielcopper/decky-romm-sync/issues/1111), where
cartridge/disc-less systems got a stray `.m3u` and a `.m3u`-suffixed folder. The gate — `system_supports_m3u` in
`adapters/es_de_config.py:259`, reading ES-DE's own `es_systems.xml` — is the thing that actually holds the line. It
gates both playlist generation and launch-file selection, which is why a foreign playlist is ignored on a system that
cannot launch one.

**How to apply:** before touching m3u generation, the launch-file detector, or the ES-DE collapse rename, assume a
playlist may already be present and may not be ours. Never narrow the gate to "only gate what we generate". If the
generator ever looks removable, the question to answer first is who else is writing the file.

## Upstream half — VERIFY BEFORE RELYING ON THIS

Observed on **RomM 5.0.0, 2026-07-28**. This is upstream implementation detail and can change in any release — it is
recorded because re-deriving it cost a full source investigation, not because it is stable.

RomM injects `f"{rom.fs_name}.m3u"` into every multi-file download zip that does not already contain one, **ungated by
platform** — so a Switch or Xbox 360 multi-file game gets a playlist too. That, not anything on our side, is the
original cause of #1111.

Re-verify with three greps against a RomM checkout:

- `backend/endpoints/roms/__init__.py:1374-1384` — the injection into the zip (and `:1436-1445` for the same on the
  zip-content-line path)
- `backend/utils/m3u.py` — `generate_m3u_content`: `.cue` files only when any exist, otherwise all files; bare relative
  filenames, `\n`-joined
- `backend/models/rom.py:588-593` — `has_m3u_file()`, the only gate: a bundled playlist is shipped verbatim and never
  regenerated

If a future check shows RomM has stopped injecting, the durable rule above still stands — a user's own library can
contain a hand-made or tool-made playlist, and other clients write them too.

Related: [[romm-siblings-cannot-identify-discs]].
