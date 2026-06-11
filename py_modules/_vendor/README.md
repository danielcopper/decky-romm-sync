# Vendored third-party packages

Decky Loader has no plugin-level package manager, so third-party runtime dependencies are vendored here and imported as
`from _vendor import <package>`. Only adapters import from `_vendor.*`. The release zip redistributes this directory, so
each package keeps its upstream `LICENSE`, and the provenance below makes updating a vendored dep a deliberate diff
rather than "diff and pray". See the `_vendor/` rules in [`CLAUDE.md`](../../CLAUDE.md).

## vdf

- **Upstream:** <https://github.com/ValvePython/vdf>
- **Version:** 3.4 — tag `v3.4`, commit `8104cb27c0b222bd802b69df58204ab389fc714c`
- **License:** MIT — see [`vdf/LICENSE`](vdf/LICENSE)
- **Local patches:** `vdf/__init__.py` — `from vdf.vdict import VDFDict` changed to `from .vdict import VDFDict`
  (relative self-import so the package resolves under `_vendor.vdf`, not a top-level `vdf`).
