# Gavel ladder conformance vectors — vendored

These JSON files are vendored verbatim from [danielcopper/romm-gavel](https://github.com/danielcopper/romm-gavel)
`vectors/ladder/`, at commit `b2e550b`.

They are the normative conformance vectors for the 409 resolution ladder (`resolve_upload_conflict`) — gavel is the
client companion contract for RomM Device Sync, itself extracted from this repo's `py_modules/domain/sync_action.py`.
`tests/domain/test_sync_action_gavel_vectors.py` runs every vector against the production kernel, so this tier proves
the kernel still conforms to the published contract.

## Updating

There is no submodule and no network access in CI — a vector change must surface as a reviewable diff in this repo.
Updating means deliberately re-copying the files from the upstream `vectors/ladder/` directory and updating the commit
reference above:

- `named-cases.json` — curated, named cases (each carries a `rationale`).
- `equivalence-classes.json` — the exhaustive equivalence-class set.

Do not reformat the copied files; they must stay byte-for-byte identical to upstream.
