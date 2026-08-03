---
paths:
  - "py_modules/bootstrap/*.py"
  - "main.py"
---

# Composition root and process boundaries

**Bootstrap (`bootstrap/`)**: `[CP]` The composition root — the only place concrete adapters meet services. `[ours]`
`adapters.py` instantiates every adapter and returns the typed bundles; `services.py` holds `WiringConfig` and turns
those bundles into service instances — protocols in, services out. `__init__.py` is namespace plus re-exports only, so
consumers write `from bootstrap import …` and never deep-import a submodule. Adapter instantiation never happens in
`main.py` — a Protocol-wrapped persister is built in `bootstrap()` and passed through `CallbackBundle`.

**Process boundaries — `main.py` vs `bootstrap/`**: `[ours]` `main.py` owns the Decky lifecycle (`_main`, `_unload`) and
the callable surface (one `async def` per `@callable`). `bootstrap/` owns adapter instantiation and service wiring. The
split is binding — no callables in `bootstrap/`, no service wiring in `main.py`.

`main.py` grows with the callable surface it describes; that is unavoidable density, not god-class, and it is
deliberately out of scope for the module-size gate.

`bootstrap/` is **not** exempt. Both modules are governed by the ~1000-LOC threshold in `scripts/check_module_size.py`
and neither is grandfathered, so each has real headroom and a hard stop. A new adapter goes into `adapters.py`, new
wiring into `services.py`; when either reaches the threshold the answer is the next split along the same seam (a wiring
module per service cluster), never an allowlist entry.
