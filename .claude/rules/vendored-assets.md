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
[`_vendor/README.md`](../../py_modules/_vendor/README.md). Where the copy is pinned against upstream's own file manifest
— `atlas`, whose `atlas.SHA256SUMS` is checked by `scripts/check_vendored_atlas.py` — the licence sits **beside** the
tree as `<package>.LICENSE`: the checked file set is an exact equality, so a licence inside the tree would be an extra
file the gate has to except.

**Compiled binaries** (no source in this repo) are vendored under `py_modules/native/` instead (inside one of the fixed
directories the Decky CLI packs into the plugin zip) — downloaded verbatim from an upstream release with a pinned
SHA-256 (CI re-verifies it; the release smoke test asserts the artifact ships in the zip), loaded by an adapter via
`ctypes` with no Python fallback; provenance and the update procedure live in
[`native/README.md`](../../py_modules/native/README.md).

**Vendored data** (no source in this repo) follows the same discipline: `defaults/bios_registry.json` is copied verbatim
from an [emu-atlas](https://github.com/danielcopper/emu-atlas) release with a pinned SHA-256
(`defaults/bios_registry.json.sha256`, CI-verified via `mise run gate`; the release smoke test asserts it ships in the
zip) — never hand-edit it, regeneration happens upstream; provenance and the update procedure live in
[`defaults/README.md`](../../defaults/README.md).

The shared rule across all three: **the artifact is a verbatim copy pinned by checksum.** Editing one in place to fix a
problem is always wrong — the fix belongs upstream, followed by a deliberate re-copy and a checksum bump.
