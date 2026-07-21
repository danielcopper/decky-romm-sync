# Packaged defaults

Reference data that ships inside the plugin. The Decky CLI flattens this directory into the plugin root at package time,
so the runtime reads these files by their bare name (no `defaults/` prefix); do not move or rename them.

## `bios_registry.json` — vendored from emu-atlas

The BIOS registry: which firmware files each platform and libretro core want, with the hashes and sizes that identify
them. Read at runtime by `FirmwareService` (via `domain/bios.py`) to classify what a platform needs and whether a local
file is the right one.

It is **vendored verbatim** from [emu-atlas](https://github.com/danielcopper/emu-atlas), where the registry and its
generator now live:

- **Upstream:** <https://github.com/danielcopper/emu-atlas>
- **Release:** `v0.1.0`
- **Upstream path:** `atlas/data/bios_registry.json`
- **Checksum:** pinned in `bios_registry.json.sha256` (SHA-256)

This repo carries only the data snapshot — there is no in-tree generator. Generation is a dev-time, offline step that
lives upstream (emu-atlas `scripts/generate_bios_registry.py`, documented in emu-atlas `atlas/data/README.md`); it
derives the registry from the libretro `libretro-core-info` and `libretro-database` checkouts. **Never hand-edit the
data here** — a manual edit would silently diverge from the released snapshot and break the checksum gate.

### How to update

1. Fetch the registry at the release tag (emu-atlas releases carry no binary assets — the tagged source tree is the
   artifact):

   ```sh
   curl -fL -o defaults/bios_registry.json \
     "https://raw.githubusercontent.com/danielcopper/emu-atlas/<tag>/atlas/data/bios_registry.json"
   ```

2. Regenerate the pinned checksum and verify it (the bare filename keeps `sha256sum -c` working from within this
   directory):

   ```sh
   cd defaults && sha256sum bios_registry.json > bios_registry.json.sha256 && sha256sum -c bios_registry.json.sha256
   ```

3. Bump the **Release** tag above.
4. Re-run the firmware tests (`tests/services/test_firmware.py`, `tests/domain/test_bios.py`) — a `required` flag flip,
   a removed entry, or a changed hash is a behavior change for consumers, so call it out in the PR description.

The checksum is re-verified by CI (`.github/workflows/ci.yml`, mirrored in `mise run gate` / `mise run lint`) and the
release smoke test asserts the registry ships in the plugin zip, so both a hand-edited snapshot and a dropped file fail
the pipeline.

## `config.json` — in-tree default

The platform-slug map and other default configuration. Unlike `bios_registry.json`, this is maintained in this repo (not
vendored) and carries no checksum gate.
