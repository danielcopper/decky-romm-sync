# Native binaries

Compiled artifacts that ship inside `py_modules/` (one of the fixed directories the Decky CLI packs into the plugin zip;
a top-level directory would be silently dropped). Unlike the Python packages under `py_modules/_vendor/`, these have no
source in this repo — they are downloaded verbatim from an upstream release, so a pinned checksum (verified in CI and at
release time) is what makes an update a deliberate, reviewable diff rather than a silent binary swap.

The `.so` is loaded by [`adapters/gavel_native.py`](../adapters/gavel_native.py) via `ctypes`; there is no Python
fallback, so a missing or mismatched artifact is a fatal, loud failure at bootstrap.

## libgavel-x86_64-linux.so

The compiled [romm-gavel](https://github.com/danielcopper/romm-gavel) core — both save-sync decision kernels: the full
per-`(rom, filename, slot)` sync action (`gavel_compute_sync_action`) and the upload-409 resolution fallback
(`gavel_resolve_upload_conflict`).

- **Upstream:** <https://github.com/danielcopper/romm-gavel>
- **Release:** `v0.4.0`
- **Architecture:** `x86_64` Linux — freestanding (zero library dependencies: no NEEDED entries, no global undefined
  symbols; upstream release CI enforces this), so it loads on any x86_64 Linux regardless of libc flavor or version
- **Checksum:** pinned in `libgavel-x86_64-linux.so.sha256` (SHA-256)

### How to update

1. Download the artifact and its checksum from a newer release:

   ```sh
   gh release download <tag> -R danielcopper/romm-gavel -p 'libgavel-x86_64-linux.so*' -D py_modules/native/
   ```

2. Verify the downloaded artifact against its checksum:

   ```sh
   cd py_modules/native && sha256sum -c libgavel-x86_64-linux.so.sha256
   ```

3. Bump the **Release** tag above.
4. Re-run the save-sync conformance + differential tests (`tests/adapters/test_gavel_native.py`,
   `tests/domain/test_sync_action_gavel_table_vectors.py`) — the shipped binary must still match the in-tree
   `domain.sync_action` kernels and the vendored gavel vectors. A gavel major bump means at least one expected outcome
   changed, so re-copy the vectors (`tests/domain/gavel_vectors/`) in the same change.

The checksum is re-verified by CI (`.github/workflows/ci.yml`) and the release smoke test asserts the `.so` is present
in the zip, so both a swapped binary and a dropped artifact fail the pipeline.
