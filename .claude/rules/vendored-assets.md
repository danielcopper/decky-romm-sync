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
