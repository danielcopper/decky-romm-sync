# Vendored copies

What this directory holds is **verbatim upstream code we do not own** — pinned by the provenance below, excluded from
our own linters and gates, and redistributed in the release zip, so each copy keeps its upstream licence (inside the
package directory, or beside it as `<package>.LICENSE` where the copy is pinned against upstream's own file manifest).
That, and not any one import mechanism, is what puts something here; keeping the copies under one root is what lets a
single set of exclusions cover them all. See the `_vendor/` rules in [`CLAUDE.md`](../../CLAUDE.md).

Everything here today is a third-party runtime dependency, vendored because Decky Loader has no plugin-level package
manager and imported as `from _vendor import <package>` — and only adapters import `_vendor.*`. The provenance entries
below make updating any of them a deliberate diff rather than "diff and pray".

## atlas

The [emu-atlas](https://github.com/danielcopper/emu-atlas) resolver — the config-aware emulator-knowledge library
extracted from this plugin.

- **Upstream:** <https://github.com/danielcopper/emu-atlas>
- **Version:** 0.10.0 — tag `v0.10.0`, from the release's `emu_atlas-0.10.0-py3-none-any.whl`
  (`sha256:57f12effac303f3cda4aab851565dceb3fa0d4582bd12d8c16b3f7660a586de1`)
- **License:** MIT — see [`atlas.LICENSE`](atlas.LICENSE)
- **Local patches:** none. Upstream made the package relocatable in
  [emu-atlas#327](https://github.com/danielcopper/emu-atlas/issues/327) — no absolute self-imports, no `files("atlas")`
  — so a verbatim copy resolves under `_vendor.atlas` with nothing to change. That is the whole premise of the checksum
  pin: unlike `vdf` below, there is no patch to reapply, so an edit here is always wrong.

The licence sits **beside** the tree as `atlas.LICENSE`, not inside it, and upstream's own release manifest is vendored
verbatim as `atlas.SHA256SUMS`. That keeps `atlas/` exactly equal to the manifest's file set, which is what lets
`scripts/check_vendored_atlas.py` assert set **equality** — nothing missing, nothing added — with no per-file
exceptions. The equality half is not optional: `sha256sum -c --ignore-missing` exits 0 after a vendored file is deleted,
so a plain checksum sweep would pass a half-copied tree.

`_vendor.atlas` is consumed by `adapters/atlas_firmware.py` alone — the firmware seam behind
`services.protocols.FirmwareResolver`. `tests/test_vendored_atlas.py` additionally imports it and asserts the pinned
version, so the copy is proven to resolve and not merely to hash correctly even if that adapter ever stops importing it.

### How to update

1. Download the wheel and the manifest from the newer tagged release:

   ```sh
   gh release download <tag> -R danielcopper/emu-atlas -p 'emu_atlas-*-py3-none-any.whl' -p SHA256SUMS -D /tmp/atlas
   ```

2. Verify the wheel against the manifest, then unpack it:

   ```sh
   cd /tmp/atlas && sha256sum -c --ignore-missing SHA256SUMS && unzip -d u emu_atlas-*-py3-none-any.whl
   ```

3. Replace `py_modules/_vendor/atlas/` with `/tmp/atlas/u/atlas/`, leaving out any `__pycache__`. Copy
   `/tmp/atlas/u/emu_atlas-<version>.dist-info/licenses/LICENSE` to `py_modules/_vendor/atlas.LICENSE` and
   `/tmp/atlas/SHA256SUMS` to `py_modules/_vendor/atlas.SHA256SUMS`.
4. Bump the **Version** bullet above and the pinned version in `tests/test_vendored_atlas.py`.
5. If the file count changed, fix the module docstring of `scripts/check_vendored_atlas.py`, which states it. The number
   is not load-bearing — the gate reads the manifest — but a stale one is worse than none.
6. Re-run the gate: `python scripts/check_vendored_atlas.py`.

The gate runs in `mise run lint` and in CI (`.github/workflows/ci.yml`), and the release smoke test asserts the tree
ships in the plugin zip, so both a tampered copy and a dropped one fail the pipeline.

## vdf

- **Upstream:** <https://github.com/ValvePython/vdf>
- **Version:** 3.4 — tag `v3.4`, commit `8104cb27c0b222bd802b69df58204ab389fc714c`
- **License:** MIT — see [`vdf/LICENSE`](vdf/LICENSE)
- **Local patches:** `vdf/__init__.py` — `from vdf.vdict import VDFDict` changed to `from .vdict import VDFDict`
  (relative self-import so the package resolves under `_vendor.vdf`, not a top-level `vdf`).

## Formatters and verbatim copies

`.githooks/pre-commit` formats **explicitly staged paths** — and an exclude written for a directory walk does not always
apply to a path handed over explicitly. That is how a formatter reaches a verbatim copy:

- **Python — already handled, do not undo it.** `pyproject.toml` sets `force-exclude = true` under `[tool.ruff]`. That
  is what makes ruff honour `extend-exclude` for the explicit paths the hook passes; without it, `ruff format` on the
  staged atlas tree rewrites 26 of its 35 Python files and breaks byte-identity against the manifest. Changing
  `line-length` does not rescue it — upstream runs no Python formatter at all, and 88 rewrites 33 files where 120
  rewrites 26 — so excluding the tree is the only answer. The checksum gate would catch it, but only after the commit —
  and removing that one line puts every future vendored package back in the same position, silently. It also stops the
  hook formatting and linting `./scripts`, which the same `extend-exclude` already asked for and CI already did; the
  hook was the last thing checking those files.
- **Markdown — still open, and nothing needs it yet.** `deno fmt` (`deno.json` includes `**/*.md`), `markdownlint-cli2`
  (the `lint:md` mise task) and `scripts/check_markdown_links.py` (which walks `git ls-files '*.md'`) all reach tracked
  markdown under `_vendor/`, and the hook reformats staged `.md` too. The emu-atlas wheel ships no markdown, so there is
  nothing to exclude today — but the next vendored package may, and reformatted prose breaks byte-identity exactly like
  reformatted code.

**The trap is not limited to vendored trees.** Any directory a _different_ formatter owns is exposed the same way, and
there it costs a whole tree rather than one file: the markdown formatter is configured for `**/*.md`, but handed `src/`
as an explicit path it reformats the TypeScript that prettier owns — 186 files in one command, every one a real diff,
and `pnpm format:check` is the only thing that notices. Hand a formatter the paths it owns, never a directory that
merely contains them.
