#!/usr/bin/env python3
"""UoW-seam nesting ban — what may not be called while a Unit of Work is open.

A Unit of Work opens a SQLite transaction with ``BEGIN IMMEDIATE`` on its own
connection. That single fact carries two separate rules, and this check
enforces both from one AST walk because the matcher is identical — only the
seam list and the failure message differ.

**Rule 1 — a seam that opens its own UoW deadlocks.** The write lock is not
re-entrant. Calling a seam that opens its OWN UoW while a UoW is already open
on the same path blocks on the nested ``BEGIN IMMEDIATE`` until
``busy_timeout`` (~5s) elapses, then raises ``database is locked``.
``FakeUnitOfWork`` in the unit tests shares no real connection, so the deadlock
is invisible there — it only surfaces on a real device. The hazard has bitten
four sites across three issues (#1047, #1134 twice, and the migration site
fixed via #1155). The seams live in :data:`SEAM_METHODS`.

**Rule 2 — a seam that touches the disk holds the write lock across the I/O.**
``BEGIN IMMEDIATE`` takes the write lock at the top of the block, so **even a
read-only UoW is a writer**. The database runs in WAL, so readers are
unaffected; any other *writer* that arrives waits on the lock for up to
``busy_timeout=5000`` and fails with ``SQLITE_BUSY`` only if it is still held
then. A directory walk or a config parse held inside a UoW therefore stalls
those writers for as long as the I/O takes. CONTEXT.md's Unit of Work entry and
ADR-0006 state the rule — a transaction wraps database reads and writes, never
file or server I/O — and until #1779 nothing detected a breach: six call sites
had drifted across it, because nothing at a call site reveals that an injected
seam touches the disk. The seams live in
:data:`IO_SEAM_METHODS`. Both the rule and this check come from reading the
code; nothing here rests on a measurement of how long any of those
transactions actually held the lock.

The established fix is the same for both: snapshot the rows a seam needs
*inside* one short UoW, close it, then call the seam *outside* any open UoW
(``services/relaunch_options_resolver.py``, ``services/cores.py``,
``services/disc.py``, ``services/downloads.py``,
``services/library/shortcut_launch_resolver.py`` are the reference shape).
Re-read anything the write then depends on: the second transaction runs on its
own connection, so the snapshot may have gone stale.

This check walks ``py_modules/services/`` and fails when either family's method
is called lexically inside an open ``with <...>uow_factory() as uow:`` block.
The two seam lists and the bare-factory open (:data:`UOW_FACTORY_SUFFIX`, which
catches a nested open under rule 1) live at the top of the file, so registering
a future seam is a one-line addition.

What the matcher cannot see
---------------------------
It is lexical and conservative (a guardrail, not a prover), and these blind
spots apply to **both** families:

* It only sees calls nested inside a ``with`` block in the same function scope
  — a seam reached through a helper called from inside the UoW is not caught,
  and a nested ``def``/``lambda`` resets the scope (a helper *defined* inside a
  UoW but *called* elsewhere is not flagged).
* It matches on surface syntax: aliasing a seam to a local
  (``fn = resolver.active_core_for_rom; fn(rom_id)``) or holding the UoW
  factory under an attribute whose name does not end in ``uow_factory`` slips
  past the name match.
* It classifies a call by its own ``func``, so a seam **passed as a bound
  method** is invisible — ``run_in_executor(None, resolver.enumerate_discs,
  install)`` inside a UoW is an attribute, not a call, and is not flagged. That
  matters more than the shape suggests: ``run_in_executor`` is how
  ``services/disc.py`` and ``services/cores.py`` reach their ``_io`` bodies in
  the first place. ``scripts/check_read_only_module.py`` records the same shape
  for its own gate.
* Both seam lists are hand-maintained. A seam whose implementation *grows* a
  UoW open or a file read later is not detected until someone adds its name.

One blind spot is **shared by both families, and only one of them closes it**.
A seam injected as a call-shaped Protocol (``__call__``, no method name of its
own) has no method name to match: the consumer writes
``self._candidate_probe(...)``, never the seam's own name. Rule 1 leaves it
open — the ``current_save_sorting`` / ``has_adoption_candidate`` entries guard
only call sites that name the method, which is the owning service's own and any
peer holding the object rather than the bound method. Rule 2's four
call-shaped seams are closed the cheap way instead: every consumer in
``services/`` binds each to one attribute — ``self._resolve_system``,
``self._system_extensions``, ``self._system_known``,
``self._firmware_folder_verdicts`` — and those attribute names are what the
list carries (``resolve_system`` is listed beside its own, being the
implementation's real method name, ``RommHttpAdapter.resolve_system``, for a
peer holding the object; the other three have no such twin). That is a
convention, not a guarantee — a consumer binding one under a different
attribute slips past, and it only works while the attribute name means one
thing. Doing the same for rule 1 means a second list of holding attributes to
keep in step, and is not built.

Conversely, matching only *attribute* calls is what keeps the ``enumerate_discs``
entry safe: the pure ``domain.disc_selection.enumerate_discs`` does no I/O of
its own, and its one consumer imports it bare. Written dotted it *would* be
flagged — the safety is in the call site's bare import, not in the name.

Seams considered and left out
-----------------------------
These were weighed for :data:`IO_SEAM_METHODS` and kept out. It is a record of
two decisions, not a survey of what touches the disk. Neither is exempt from the
rule — a UoW held across either is a breach — and the reason is not that their
I/O matters less:

* ``DirectoryFileListerFn`` — a directory listing, and its consumer binds it to
  ``self._list_files``. Its call-shaped siblings are *in* the list, matched by
  the attribute they are bound to, so the mechanism plainly reaches this shape;
  what stops this one is the name. ``_list_files`` says nothing about which seam
  it holds — any class might bind it to something unrelated — so an entry would
  key the gate on a coincidence, where ``_system_extensions`` and
  ``_resolve_system`` each mean one thing.
* ``RetroDeckPaths``'s path getters sit behind a 30-second TTL cache
  (``adapters/retrodeck_paths.py``), so a call is usually a dict lookup and a
  ban would fire mostly where nothing is spent — which teaches writers to reach
  for a pragma instead of looking. The caveat is that only a successfully-read
  config is ever cached, and the TTL guard tests the cached value first, so on a
  machine without RetroDECK every getter reopens.

The escape hatch is a trailing comment on the seam-call line:

    self._active_core.active_core_for_rom(rom_id)  # pragma: no uow-check

**One pragma covers both families,** because it suppresses the *line*, not a
named rule — so a line naming more than one seam, such as
``get_emulator_options(self._resolve_system(slug))``, is silenced by the single
comment on it. A rule-named spelling would only ask the writer to restate what
the failure message already told them.

Exit 0 on no findings, exit 1 if any findings (one line per finding).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "py_modules" / "services"

# --- Rule 1: seams that open their own Unit of Work ----------------------
# Method names whose implementation opens its OWN Unit of Work. Calling any
# of these while a UoW is already open on the same SQLite connection
# re-enters the non-reentrant ``BEGIN IMMEDIATE`` and deadlocks. Matched by
# method name regardless of the receiver attribute — the names are distinctive
# to their seam, so this can't be dodged by renaming the holding field. A new
# seam is a one-line addition here. The two marked ``via a …Fn`` reach their
# only cross-service consumer as an injected bound method, which this matcher
# does not see (module docstring); their entries hold for by-name call sites.
SEAM_METHODS: frozenset[str] = frozenset(
    {
        "active_core_for_rom",  # ActiveCoreResolver (services/active_core_resolver.py)
        "active_emulator_for_rom",  # ActiveCoreResolver (services/active_core_resolver.py)
        "current_save_sorting",  # RomInfoService / SaveService — via a SaveSortingProvider
        "has_adoption_candidate",  # RomAdoptionService — via an AdoptionCandidateProbeFn
        "installed_relaunch_items",  # RelaunchOptionsResolver (services/relaunch_options_resolver.py)
        "launch_path_for_rom",  # RelaunchOptionsResolver (services/relaunch_options_resolver.py)
        "relaunch_item_for_rom",  # RelaunchOptionsResolver (services/relaunch_options_resolver.py)
    }
)

# --- Rule 2: seams that touch the filesystem -----------------------------
# Method names whose implementation reads the disk. None of them opens a UoW,
# so nothing deadlocks — the cost is duration: the UoW's BEGIN IMMEDIATE holds
# the write lock across the whole walk, and any other writer that arrives gives
# up at busy_timeout. Each entry names the Protocol it belongs to and the I/O it
# performs. What is deliberately NOT here, and why, is in the module docstring.
IO_SEAM_METHODS: frozenset[str] = frozenset(
    {
        # DiscResolver (services/protocols/cross_service.py) — recursive walk of
        # the ROM's install directory, plus an ES-DE config read for the system's
        # supported extensions.
        "enumerate_discs",
        # DiscResolver — the same recursive walk, resolving the persisted disc
        # pin over it.
        "resolve_for_install",
        # CoreInfoProvider (services/protocols/paths.py) — all four reads of
        # RetroDECK's ES-DE configuration. Each re-probes the flatpak install
        # roots for ITS file and re-stats it before it may answer from the parse
        # cache: es_systems.xml for the first three, es_find_rules.xml for
        # resolve_sandbox_launcher. get_emulator_options touches both — it globs
        # every option's emulator install through the find rules. Listing one of
        # the four would be arbitrary: services/firmware/status.py calls two of them
        # inside one loop, and services/active_core_resolver.py the other two.
        "get_active_core",
        "get_default_emulator",
        "get_emulator_options",
        "resolve_sandbox_launcher",
        # SystemResolver (services/protocols/paths.py) — parses the plugin's OWN
        # bundled config.json (plugin root, else defaults/config.json) for its
        # platform_map. Implemented on the RomM HTTP adapter, which the name makes
        # easy to misread twice over: it does no network work, and the file is not
        # RetroDECK's retrodeck.json. It is also the odd one out — the adapter
        # memoises the map for the life of the process, so exactly one call ever
        # opens the file. The entry earns its place because that one call can land
        # inside a UoW. The Protocol is call-shaped, so `_resolve_system` — the
        # attribute every consumer in services/ binds it to — is listed beside
        # `resolve_system`, the implementation's own method name.
        "resolve_system",
        "_resolve_system",
        # FirmwareFolderVerdictFn (services/protocols/paths.py) — lists one
        # core's declared folder and reads every candidate inside it the way the
        # core does (0.26 s for LRPS2 on the reference machine, against 0.24 s
        # for the whole machine's unverified inventory). The fourth call-shaped
        # seam, so the list carries the attribute every consumer in services/
        # binds it to, as it does for the other three.
        "_firmware_folder_verdicts",
        # SystemSupportedExtensionsFn / SystemKnownFn (services/protocols/paths.py)
        # — two questions to es_systems.xml, through the same every-call flatpak
        # probe as the CoreInfoProvider reads above. Both Protocols are
        # call-shaped, and unlike SystemResolver there is no method name a
        # service could write beside the attribute: the implementations
        # (CoreResolver.get_supported_extensions / .is_known_system) are on no
        # Protocol a service holds. So the attribute is all there is, and it is
        # matchable because each of these means one thing in the tree.
        "_system_extensions",
        "_system_known",
    }
)

# A Unit of Work is opened by *calling* a factory whose final name segment
# ends with this suffix — ``self._uow_factory()``, ``config.uow_factory()``,
# a bare ``uow_factory()``. Used both to recognise the enclosing ``with``
# block AND to catch a nested factory open inside one.
UOW_FACTORY_SUFFIX = "uow_factory"

ESCAPE_HATCH = "pragma: no uow-check"

# The two remedies are one sentence apart, but the hazard behind them is not:
# rule 1 hangs this operation, rule 2 holds every other writer up. A reader who
# hits either message has to be able to tell which rule they broke.
_DEADLOCK_REMEDY = (
    "snapshot inside the UoW, close it, "
    "then resolve outside (the nested BEGIN IMMEDIATE deadlocks → 'database is locked')"
)
_WRITE_LOCK_REMEDY = (
    "snapshot inside the UoW, close it, "
    "then do the I/O outside (BEGIN IMMEDIATE holds the write lock even for a "
    "read-only UoW, so every other writer in the plugin waits for as long as the I/O "
    "takes, up to busy_timeout)"
)


def _final_name(func: ast.expr) -> str | None:
    """Return the final identifier of a call target, or None.

    ``self._uow_factory`` -> ``_uow_factory``; ``config.uow_factory`` ->
    ``uow_factory``; ``uow_factory`` -> ``uow_factory``; a subscript/other
    expression -> None.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_uow_opener(node: ast.expr) -> bool:
    """Return True when *node* is a call that opens a Unit of Work."""
    if not isinstance(node, ast.Call):
        return False
    name = _final_name(node.func)
    return name is not None and name.endswith(UOW_FACTORY_SUFFIX)


def _classify(node: ast.Call) -> tuple[str, str] | None:
    """Return ``(detail, remedy)`` for a hazardous call, or None.

    Only attribute calls are considered for the two seam families. A
    module-level function sharing a seam's name
    (``domain.disc_selection.enumerate_discs``) is therefore missed while its
    call site imports it bare, which is how the one in the tree is written; a
    dotted call to the same function would be flagged.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in SEAM_METHODS:
            return f"UoW-opening seam '.{func.attr}(...)'", _DEADLOCK_REMEDY
        if func.attr in IO_SEAM_METHODS:
            return f"file-I/O seam '.{func.attr}(...)'", _WRITE_LOCK_REMEDY
    if _is_uow_opener(node):
        return f"nested UoW open '{_final_name(func)}(...)'", _DEADLOCK_REMEDY
    return None


def _finding_for_call(node: ast.Call, source_lines: list[str], rel: str) -> str | None:
    """Classify one call reached while a UoW is open. None = not a hazard."""
    classified = _classify(node)
    if classified is None:
        return None
    detail, remedy = classified

    line_idx = node.lineno - 1
    if 0 <= line_idx < len(source_lines) and ESCAPE_HATCH in source_lines[line_idx]:
        return None

    return f"{rel}:{node.lineno}:{node.col_offset} {detail} called while a UoW is open — {remedy}"


def _scan(node: ast.AST, in_uow: bool, source_lines: list[str], rel: str, findings: list[str]) -> None:
    """Walk *node*, flagging seam calls reached while *in_uow* is True.

    ``in_uow`` tracks whether the current position is lexically inside an open
    ``with <...>uow_factory()`` block within the same function scope. A nested
    ``def``/``lambda`` starts a fresh scope (``in_uow`` resets to False).
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        # A nested def/lambda is a new scope: its body does not execute inside
        # the enclosing UoW even when lexically nested. Reset so a helper
        # *defined* inside a UoW is not mistaken for a call inside it.
        for child in ast.iter_child_nodes(node):
            _scan(child, False, source_lines, rel, findings)
        return

    if isinstance(node, (ast.With, ast.AsyncWith)):
        opens_uow = False
        for item in node.items:
            # The context expression is evaluated in the enclosing scope, so a
            # factory open here that is itself nested inside another UoW counts.
            _scan(item.context_expr, in_uow, source_lines, rel, findings)
            if _is_uow_opener(item.context_expr):
                opens_uow = True
        body_in_uow = in_uow or opens_uow
        for stmt in node.body:
            _scan(stmt, body_in_uow, source_lines, rel, findings)
        return

    if isinstance(node, ast.Call):
        if in_uow:
            finding = _finding_for_call(node, source_lines, rel)
            if finding is not None:
                findings.append(finding)
        for child in ast.iter_child_nodes(node):
            _scan(child, in_uow, source_lines, rel, findings)
        return

    for child in ast.iter_child_nodes(node):
        _scan(child, in_uow, source_lines, rel, findings)


def scan_source(source: str, filename: str = "<source>") -> list[str]:
    """Return every in-UoW seam finding in one module's *source* text."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    source_lines = source.splitlines()
    findings: list[str] = []
    for node in tree.body:
        _scan(node, False, source_lines, filename, findings)
    return findings


def find_violations(services_dir: Path = SERVICES_DIR) -> list[str]:
    """Walk *services_dir* and return every in-UoW seam finding."""
    findings: list[str] = []
    if not services_dir.is_dir():
        return findings
    for path in sorted(services_dir.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        findings.extend(scan_source(source, rel))
    return findings


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    findings = find_violations(SERVICES_DIR)
    if findings:
        for line in findings:
            print(line)
        # Only summarise the rules that actually fired, so the closing advice
        # matches the findings above it.
        if any(_DEADLOCK_REMEDY in line for line in findings):
            print()
            print(
                "ERROR: a UoW-opening seam (ActiveCoreResolver / RelaunchOptionsResolver / "
                "uow_factory) must not be called while a UoW is open on the same path "
                "(CLAUDE.md → Invariant register). Snapshot inside the UoW, close it, then "
                "resolve outside."
            )
        if any(_WRITE_LOCK_REMEDY in line for line in findings):
            print()
            print(
                "ERROR: a file-I/O seam (DiscResolver / CoreInfoProvider / SystemResolver) "
                "must not be called while a UoW is open — a Unit of Work wraps database "
                "reads and writes only, never file or server I/O (CLAUDE.md → Invariant "
                "register, CONTEXT.md → Unit of Work, ADR-0006). Snapshot inside the UoW, "
                "close it, then do the I/O outside."
            )
        return 1
    print(f"OK: no nested UoW-seam calls in {SERVICES_DIR.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
