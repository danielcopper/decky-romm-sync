---
paths:
  - "py_modules/bootstrap.py"
  - "main.py"
---

# Composition root and process boundaries

**Bootstrap (`bootstrap.py`)**: `[CP]` The composition root — the only place concrete adapters meet services. `[ours]`
`WiringConfig` holds the wiring; protocols in, services out. Adapter instantiation never happens in `main.py` — a
Protocol-wrapped persister is built in `bootstrap()` and passed through `CallbackBundle`.

**Process boundaries — `main.py` vs `bootstrap.py`**: `[ours]` `main.py` owns the Decky lifecycle (`_main`, `_unload`)
and the callable surface (one `async def` per `@callable`). `bootstrap.py` owns adapter instantiation and service
wiring. The split is binding — no callables in `bootstrap.py`, no service wiring in `main.py`.

`main.py` grows with the callable surface it describes; that is unavoidable density, not god-class, and it is
deliberately out of scope for the module-size gate.

`bootstrap.py` is **not** exempt. It is already past the ~700-LOC split trigger and is pinned at its current size by
`scripts/check_module_size.py`, so it cannot grow further: new wiring means splitting it into
`bootstrap/{adapters,services}.py` first.
