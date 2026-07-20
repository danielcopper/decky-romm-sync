# Gavel conformance vectors — vendored

These JSON files are vendored verbatim from [danielcopper/romm-gavel](https://github.com/danielcopper/romm-gavel)
`vectors/`, at commit `195e43b`. The layout mirrors upstream — one subdirectory per vector family:

- `ladder/` — the 409 resolution ladder (`resolve_upload_conflict`), run by
  `tests/domain/test_sync_action_gavel_vectors.py`.
  - `named-cases.json` — curated, named cases (each carries a `rationale`).
  - `equivalence-classes.json` — the exhaustive equivalence-class set.
- `decision-table/` — the full per-`(rom, filename, slot)` sync decision (`compute_sync_action`), run by
  `tests/domain/test_sync_action_gavel_table_vectors.py`.
  - `named-cases.json` — curated cases across every branch of the decision table (each carries a `rationale`).

They are the normative conformance vectors for the save-sync decision kernels — gavel is the client companion contract
for RomM Device Sync, itself extracted from this repo's `py_modules/domain/sync_action.py`. Each test runs every vector
in its family against the production kernel, so this tier proves the kernel still conforms to the published contract.

## Updating

There is no submodule and no network access in CI — a vector change must surface as a reviewable diff in this repo.
Updating means deliberately re-copying the files from the matching upstream `vectors/<family>/` directory and updating
the commit reference above. Do not reformat the copied files; they must stay byte-for-byte identical to upstream.
