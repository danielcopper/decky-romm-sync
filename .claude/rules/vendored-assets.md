---
paths:
  - "py_modules/_vendor/**"
  - "py_modules/native/**"
  - "defaults/**"
---

# Vendored code, binaries, and data `[ours]`

**Vendored deps (`_vendor/`)**: Third-party runtime deps are vendored under `py_modules/_vendor/<package>/` (Decky has
no plugin-level package manager) and imported as `from _vendor import <package>`. Only adapters import `_vendor.*`;
services/domain/lib stay third-party-free (`domain-stdlib-only` contract in `.importlinter`). `_vendor/` is excluded
from ruff, basedpyright, and Sonar. Every vendored package ships its upstream `LICENSE` and a provenance entry in
[`_vendor/README.md`](../../py_modules/_vendor/README.md).

**One manifest per tree.** `py_modules/_vendor/<package>/` is pinned by the `<package>.SHA256SUMS` beside it, and
`scripts/check_vendored_trees.py` fails on a package directory that has none — the manifest is discovered, never named
in the script, so vendoring a package without dropping one next to it breaks the build rather than leaving an unguarded
tree beside a guarded one. The manifest is upstream's own release manifest where the copy is verbatim (`atlas`), or one
generated from the tree where the copy carries a documented local patch (`vdf`); `_vendor/README.md` records which kind
each one is, and both are asserted the same way. Where it is a wheel's manifest the licence sits **beside** the tree as
`<package>.LICENSE`: the checked file set is an exact equality, so a licence inside the tree would be an extra file the
gate has to except.

**Nothing in this repo's toolchain runs Decky Loader's frozen Python.** The venv, the tests, the type-checker and the
linters all run ordinary CPython, so a vendored package's assumptions about the standard library are invisible here and
surface only when the plugin loads on a device. Two shapes have actually occurred, both recorded in emu-atlas and both
fixed upstream in the release vendored today — but the first one hit this plugin's own code months before any vendoring,
and that fix is still in the tree:

- **A stdlib wrapper package a frozen build drops while the extension it wraps ships.** `xml.etree` is Python source
  over the expat extension; PyInstaller bundles only what its analysis reaches, so `import xml.etree.ElementTree` raises
  `ModuleNotFoundError: No module named 'xml.etree'` on Decky's bundle while expat itself sits in `lib-dynload`. This is
  **not** a hypothetical import from someone else's history: #57 shipped this plugin's own `es_systems.xml` parsing on
  `xml.etree` and had to rewrite both readers onto expat's callback API, which is why `adapters/es_de_config.py` still
  parses through `xml.parsers.expat` today. Fixed in emu-atlas the same way — see the vendored `atlas/_xml.py`, which
  states the whole account, and [emu-atlas#339](https://github.com/danielcopper/emu-atlas/issues/339). The vendored copy
  then carried it straight back in: atlas 0.5.0 landed here (#1805) still importing `xml.etree` at module level, and
  #1807 — the change that wired the resolver in and bumped past it — hit the raise on a device, at bootstrap, before
  that bump. A device test is what found it; no gate here would have.
- **A package name passed as a string rather than imported.** `importlib.resources.files("<pkg>")` names the package in
  a string literal, so it is invisible to any import rewrite and to every grep for import statements. Fixed in emu-atlas
  by making a directory copy resolve under any parent package
  ([emu-atlas#327](https://github.com/danielcopper/emu-atlas/issues/327)).

The consequence for this repo is the actionable half: **vendoring a package is a device-test trigger.** A green
`mise run gate` says the copy hashes correctly and imports under CPython; it says nothing about whether it imports under
Decky's interpreter. How far a load-time failure spreads is a property of the wiring, not of vendoring: today `main.py`
→ `bootstrap/adapters.py` → `adapters/atlas_firmware.py` → `from _vendor.atlas import …` are all module-level imports,
so a raise inside the vendored tree takes the whole plugin down rather than one feature. A package reached only behind a
lazy import would cost just the path that reaches it.

**Compiled binaries** (no source in this repo) are vendored under `py_modules/native/` instead (inside one of the fixed
directories the Decky CLI packs into the plugin zip) — downloaded verbatim from an upstream release with a pinned
SHA-256 (CI re-verifies it; the release smoke test asserts the artifact ships in the zip), loaded by an adapter via
`ctypes` with no Python fallback; provenance and the update procedure live in
[`native/README.md`](../../py_modules/native/README.md).

**Vendored data** used to be a third category — `defaults/bios_registry.json`, a firmware snapshot copied from an
emu-atlas release under its own checksum. It is gone with the swap to the live resolver, and nothing in `defaults/` is
vendored today; `config.json` is maintained in this repo. `defaults/README.md` records what left and why, so the next
person to reach for a snapshot there finds the reason it was not the answer.

The shared rule across the categories that remain: **the artifact is an upstream copy pinned by checksum** — verbatim,
or verbatim plus the local patch its provenance entry documents. Editing one in place to fix a problem is always wrong —
the fix belongs upstream, followed by a deliberate re-copy and a checksum bump.
