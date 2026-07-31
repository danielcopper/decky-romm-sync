---
name: release-zip-defaults
description: What ships in the release zip vs the repo layout. `config.json` / `bios_registry.json` / `core_defaults.json` appear at the ROOT of the release zip even though they live under `defaults/` in the repo — that's decky's `defaults/` convention: its contents are copied to the plugin root on install. Backend reads them with a dual path: root first (release), then `defaults/` (dev, where `mise run deploy` rsyncs them into a `defaults/` subdir). See firmware.py, es_de_config.py, adapters/romm/http.py. Vendored vdf lib ships `vdf/__init__.py` + `vdf/vdict.py` only — `*.dist-info` was removed (#764) as unused pip metadata (vdf hardcodes `__version__`; nothing reads `importlib.metadata`).
    type: project
---

# What ships in the release zip — `defaults/` flattens to plugin root

`config.json` / `bios_registry.json` / `core_defaults.json` appear at the **root** of the release zip even though they
live under `defaults/` in the repo. That is decky's `defaults/` convention: its contents are copied to the plugin root
on install. The backend reads them with a dual path — root first (release), then `defaults/` (dev, where
`mise run deploy` rsyncs them into a `defaults/` subdir). See `firmware.py`, `es_de_config.py`, `adapters/romm/http.py`.
These are required, not cruft. The vendored `vdf` lib ships `vdf/__init__.py` + `vdf/vdict.py` only — its `*.dist-info`
was removed (#764) as unused pip metadata (`vdf` hardcodes `__version__`; nothing reads `importlib.metadata`).
