# emu-atlas conformance vectors — vendored

These JSON files are vendored verbatim from [danielcopper/emu-atlas](https://github.com/danielcopper/emu-atlas)
`vectors/machines/`, at release tag `v0.1.0`. The layout mirrors upstream — one subdirectory per vector family:

- `machines/` — fixture machines in, detected installations + save placements out, run by
  `tests/test_atlas_machine_vectors.py`.
  - `named-cases.json` — the 16 curated machine vectors (each carries a `rationale`).

emu-atlas is the config-aware emulator-knowledge library extracted from this plugin; its machine vectors are the
normative contract for where RetroArch/RetroDECK installs keep saves, and the save-placement expectations are
oracle-derived from this repo's `domain/save_path.resolve_save_dir` / `compute_local_save_target`. The test proves the
plugin's own kernel still agrees with the published contract where the two overlap.

## Updating

There is no submodule and no network access in CI — a vector change must surface as a reviewable diff in this repo.
Updating means deliberately re-copying the files from the matching upstream `vectors/<family>/` directory and bumping
the release tag above. Do not reformat the copied files; they must stay byte-for-byte identical to upstream. Never edit
a vector to make the kernel pass — a genuine divergence is a finding to triage, not a vector to rewrite.
