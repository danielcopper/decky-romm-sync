"""Every emitted ``sync_progress`` frame that stops a run names a terminal stage.

The QAM panel derives "a sync run is in flight" from the emitted frame's
``running`` flag, and keys the run's end — the status line the run gets, the
live-ETA teardown, the stats and session-budget re-reads — on the frame's
*stage* (``src/components/MainPage.tsx``). A frame emitted with ``running``
false and a non-terminal stage would therefore collapse the in-progress rows
while ending nothing: no completion line, no re-read, and a coarse bar frozen
where it stood. Neither side's call site shows the coupling, and the frontend
cannot defend against it — a bare ``running: false`` is indistinguishable from
the panel's own retraction of an optimistic start.

The scan is structural rather than behavioural because the rule is about every
path that *can* emit, error paths included, not the ones a fixture happens to
provoke. It reaches three producer shapes across ``py_modules/services``, all
three of which exist today:

* a call to the orchestrator's ``emit_progress``, which the reporter reaches
  through its injected ``_emit_progress`` and ``services/artwork.py`` through an
  ``emit_progress`` callable passed in per operation;
* a direct write of the frame to ``sync_progress``, which the cancelled finalize
  and the error path use because they build the snapshot themselves;
* a frame handed straight to the emit primitive as ``_emit("sync_progress", {…})``.

Where the enforcement ends, stated plainly because the invariant register cites
this file: the scan reads call sites and dict literals, so a frame assembled by
a helper it cannot follow, or emitted through an aliased callable, is outside it.
Two such indirections exist today and are inert rather than holes — the
parameterized snapshot inside ``emit_progress`` itself (its ``running`` and
``stage`` are its parameters, and every caller of it IS scanned) and the
fetcher's late-bound proxy in ``services/library/service.py``, which forwards
whatever its own — scanned — callers passed it.

:class:`TestScanScope` pins the rest of the scope so it cannot shrink unnoticed:
that the scan still reaches every module producing frames today, that no producer
lives outside its root, and that ``services/prune``'s unrelated method of the same
name lands inert rather than as a false offender in a foreign API.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVICES_ROOT = _REPO_ROOT / "py_modules" / "services"
_TERMINAL_STAGES = frozenset({"DONE", "CANCELLED", "ERROR"})
_EMIT_PROGRESS_NAMES = frozenset({"emit_progress", "_emit_progress"})
_EMIT_NAMES = frozenset({"emit", "_emit"})
_PROGRESS_EVENT = "sync_progress"

# The modules that produce frames today. Named so a narrowing of the scan — back
# to one directory, or to a non-recursive glob — fails loudly instead of quietly
# shrinking what the rule covers.
_KNOWN_PRODUCERS = frozenset(
    {
        "services/library/sync_orchestrator.py",
        "services/library/reporter.py",
        "services/artwork.py",
    }
)


class _Site(NamedTuple):
    """One frame producer: where it is, and how it names ``running`` / the stage."""

    where: str
    stage: str | None
    stops_the_run: bool

    @property
    def module(self) -> str:
        return self.where.rsplit(":", 1)[0]


def _modules() -> Iterator[tuple[str, ast.Module]]:
    """Every module under ``py_modules/services``, recursively."""
    for path in sorted(_SERVICES_ROOT.rglob("*.py")):
        yield (
            path.relative_to(_SERVICES_ROOT.parent).as_posix(),
            ast.parse(path.read_text(encoding="utf-8")),
        )


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _dict_value(node: ast.Dict, key: str) -> ast.expr | None:
    return next(
        (v for k, v in zip(node.keys, node.values, strict=True) if isinstance(k, ast.Constant) and k.value == key),
        None,
    )


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _stage_name(node: ast.expr | None) -> str | None:
    """``SyncStage.DONE`` or ``SyncStage.DONE.value`` -> ``"DONE"``; else ``None``."""
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "SyncStage":
        return node.attr
    return None


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_progress_event(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == _PROGRESS_EVENT


def _frame_from_dict(where: str, frame: ast.Dict) -> _Site:
    return _Site(where, _stage_name(_dict_value(frame, "stage")), _is_false(_dict_value(frame, "running")))


def _emit_progress_sites() -> list[_Site]:
    """Every call that emits a frame through an ``emit_progress`` helper."""
    sites: list[_Site] = []
    for module, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) not in _EMIT_PROGRESS_NAMES:
                continue
            stage = node.args[0] if node.args else _keyword(node, "stage")
            sites.append(_Site(f"{module}:{node.lineno}", _stage_name(stage), _is_false(_keyword(node, "running"))))
    return sites


def _snapshot_write_sites() -> list[_Site]:
    """Every direct write of a frame to ``…sync_progress``, which is then emitted."""
    sites: list[_Site] = []
    for module, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            if any(isinstance(t, ast.Attribute) and t.attr == "sync_progress" for t in node.targets):
                sites.append(_frame_from_dict(f"{module}:{node.lineno}", node.value))
    return sites


def _inline_emit_sites() -> list[_Site]:
    """Every frame handed to the emit primitive inline: ``_emit("sync_progress", {…})``."""
    sites: list[_Site] = []
    for module, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) not in _EMIT_NAMES:
                continue
            if len(node.args) < 2:
                continue
            payload = node.args[1]
            if _is_progress_event(node.args[0]) and isinstance(payload, ast.Dict):
                sites.append(_frame_from_dict(f"{module}:{node.lineno}", payload))
    return sites


def _frame_sites() -> list[_Site]:
    return [*_emit_progress_sites(), *_snapshot_write_sites(), *_inline_emit_sites()]


def _parameter_default(fn: ast.AsyncFunctionDef, name: str) -> ast.expr | None:
    params = [*fn.args.posonlyargs, *fn.args.args]
    first_defaulted = len(params) - len(fn.args.defaults)
    for index, param in enumerate(params):
        if param.arg == name and index >= first_defaulted:
            return fn.args.defaults[index - first_defaulted]
    return next((d for kw, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True) if kw.arg == name), None)


def _emit_progress_definition() -> ast.AsyncFunctionDef:
    for module, tree in _modules():
        if module != "services/library/sync_orchestrator.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "emit_progress":
                return node
    raise AssertionError("emit_progress is not defined in services/library/sync_orchestrator.py")


class TestTerminalFrameContract:
    def test_every_emitted_frame_that_stops_a_run_names_a_terminal_stage(self):
        offenders = [s for s in _frame_sites() if s.stops_the_run and s.stage not in _TERMINAL_STAGES]
        assert offenders == [], (
            "a sync_progress frame emitted with running=False must name a terminal stage "
            f"({sorted(_TERMINAL_STAGES)}), or the QAM panel ends the run's UI without ending the run: {offenders}"
        )

    def test_a_terminal_stage_is_never_emitted_as_a_run_still_going(self):
        # The converse half, and the reason the panel can key its teardown on the
        # stage alone: a terminal stage always comes with the run stopped.
        offenders = [s for s in _frame_sites() if s.stage in _TERMINAL_STAGES and not s.stops_the_run]
        assert offenders == [], f"a terminal stage was emitted without running=False: {offenders}"

    def test_a_frame_keeps_the_run_running_unless_it_says_otherwise(self):
        # An omitted ``running`` is what every mid-run frame passes — including the
        # cover frames in services/artwork.py, which never name it — so the default
        # decides what a caller that leaves it out emits.
        default = _parameter_default(_emit_progress_definition(), "running")
        assert isinstance(default, ast.Constant), f"``running``'s default is not a literal: {default}"
        assert default.value is True, f"``running`` defaults to {default.value}, so an omitted one could stop a run"


class TestScanScope:
    """What the scan reaches, pinned so a narrowing of it cannot pass unnoticed."""

    def test_it_reaches_every_module_that_produces_frames_today(self):
        # Non-vacuity with teeth: an empty scan, or one narrowed back to a single
        # non-recursive directory, satisfies every assertion above. artwork.py is
        # the one that a services/library-only glob loses — it emits through an
        # ``emit_progress`` callable the orchestrator passes in.
        reached = {site.module for site in _frame_sites()}
        assert reached >= _KNOWN_PRODUCERS, f"the scan no longer reaches {sorted(_KNOWN_PRODUCERS - reached)}"

    def test_it_reaches_all_three_producer_shapes(self):
        assert len([s for s in _emit_progress_sites() if s.stops_the_run]) >= 4
        assert len([s for s in _snapshot_write_sites() if s.stops_the_run]) >= 2
        # The inline shape has no instance today — the two hand-built frames are
        # written to ``sync_progress`` first — so this pins the collector itself
        # against a fixture rather than against production code.
        parsed = ast.parse('await self._emit("sync_progress", {"running": False, "stage": SyncStage.FETCHING.value})')
        call = next(n for n in ast.walk(parsed) if isinstance(n, ast.Call) and _called_name(n) == "_emit")
        assert isinstance(call.args[1], ast.Dict)
        site = _frame_from_dict("fixture:1", call.args[1])
        assert site.stops_the_run, "the inline collector missed running=False"
        assert site.stage == "FETCHING", f"the inline collector read the stage as {site.stage}"

    def test_no_frame_producer_lives_outside_the_scanned_root(self):
        # The root is the other half of the scope: a producer added outside
        # py_modules/services would be invisible rather than wrong. Nothing emits a
        # frame from there today — main.py and the rest of py_modules reach the
        # sync only through the service — and this is what says so if that changes.
        outside: list[str] = []
        candidates = [_REPO_ROOT / "main.py", *(_REPO_ROOT / "py_modules").rglob("*.py")]
        for path in sorted(candidates):
            if _SERVICES_ROOT in path.parents or "_vendor" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node)
                emits_frame = name in _EMIT_NAMES and bool(node.args) and _is_progress_event(node.args[0])
                if name in _EMIT_PROGRESS_NAMES or emits_frame:
                    outside.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}")
        assert outside == [], f"a sync_progress producer outside the scan's root is unchecked by it: {outside}"

    def test_a_foreign_emit_progress_is_inert_rather_than_a_false_offender(self):
        # ``services/prune`` has its own ``emit_progress`` — a different event
        # (prune_progress), a plain-string stage, no ``running`` flag, and
        # ``run_id`` where this one takes the stage. Matching by name reaches those
        # sites, so they must land as "names no stage, stops no run": inert for
        # both assertions above rather than offenders in a foreign API.
        foreign = [s for s in _emit_progress_sites() if s.module.startswith("services/prune/")]
        assert foreign, "expected the prune emit_progress call sites to be scanned"
        assert all(s.stage is None and not s.stops_the_run for s in foreign), foreign
