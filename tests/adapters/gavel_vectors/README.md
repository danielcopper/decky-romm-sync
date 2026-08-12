# Gavel conformance vectors — vendored

These JSON files are vendored verbatim from [danielcopper/romm-gavel](https://github.com/danielcopper/romm-gavel)
`vectors/`, at release tag `v1.0.1`. The layout mirrors upstream — one subdirectory per vector family:

- `ladder/` — the 409 resolution ladder (`gavel_resolve_upload_conflict`), run against the compiled core by
  `tests/adapters/test_gavel_native.py`.
  - `named-cases.json` — curated, named cases (each carries a `rationale`).
  - `equivalence-classes.json` — the exhaustive equivalence-class set.
- `decision-table/` — the full per-`(rom, filename, slot)` sync decision (`gavel_compute_sync_action`), run against the
  compiled core by `tests/adapters/test_gavel_native_table_vectors.py`.
  - `named-cases.json` — curated cases across every branch of the decision table (each carries a `rationale`).

They are the normative conformance vectors for the save-sync decisions — gavel is the client companion contract for RomM
Device Sync, itself extracted from this plugin. Both decisions are made by the vendored core and nowhere else, so every
vector runs against the binary the plugin actually ships, and this tier is what proves it still conforms to the
published contract.

## Updating

There is no submodule and no network access in CI — a vector change must surface as a reviewable diff in this repo.
Updating means deliberately re-copying the files from the matching upstream `vectors/<family>/` directory and updating
the release tag above — upstream's `CONTRIBUTING.md` asks clients to pin a tag, not a raw commit, because a changed
expected value has to arrive as a version bump. Do not reformat the copied files; they must stay byte-for-byte identical
to upstream. The vendored `py_modules/native/libgavel-x86_64-linux.so` is pinned to the same release: bump the two
together, never one alone.
