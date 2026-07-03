"""Classify ES-DE ``<command>`` strings into safely-bakeable emulator options.

Given the label + command text of one ES-DE ``<command>``, decide whether the
plugin can bake it into a Steam shortcut's ``-e`` override — a real emulator
invocation ending in ``%ROM%`` — and whether it is a RetroArch libretro core or
a standalone emulator. The system-layer default is the first *safely-bakeable*
command in document order; the emulator picker offers every command annotated
with its bakeability and, when it cannot be baked, the reason.

Pure compute only — takes the already-parsed ``(label, text)`` pairs the
:mod:`adapters.es_de_config` reader lifts from ``es_systems.xml`` and returns
value objects. The launch-bake itself (``EmulatorInvocation`` → the ``-e``
string) lives in :mod:`domain.shortcut_data`; this module only decides *which*
command becomes that invocation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from domain.shortcut_data import EmulatorInvocation

# A RetroArch libretro command in the exact shape the plugin bakes: the
# RetroArch launcher, ``-L`` the core ``.so``, then ``%ROM%``. The captured
# group is the bare libretro core name (no ``.so``).
_LIBRETRO_RE = re.compile(r"^%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/([\w-]+_libretro)\.so %ROM%$")

_PLACEHOLDER_RE = re.compile(r"%[^%]+%")

# Placeholders ES-DE's ``run_game.sh`` resolves that a baked ``-e`` override can
# safely carry verbatim. ``%EMULATOR_*%`` (any emulator binary token, e.g.
# ``%EMULATOR_RPCS3%``) is accepted by prefix, not listed here. ``%STARTDIR%``
# is "known" (so it is not flagged as an unknown placeholder) but is rejected on
# its own by the dedicated startdir rule — run_game.sh parses-but-drops it, so a
# baked command that relies on it would launch from the wrong directory.
_KNOWN_PLACEHOLDERS = frozenset(
    {
        "%ROM%",
        "%CORE_RETROARCH%",
        "%GAMEDIR%",
        "%GAMEDIRRAW%",
        "%ROMPATH%",
        "%BASENAME%",
        "%FILENAME%",
        "%ROMRAW%",
        "%STARTDIR%",
    }
)


@dataclass(frozen=True)
class EmulatorOption:
    """One ES-DE ``<command>`` classified for launch-bakeability.

    ``label`` is ES-DE's display label — the pick key the per-game/per-platform
    override stores. ``kind`` is ``"libretro"`` (a RetroArch core; ``core_so``
    is its bare name) or ``"standalone"`` (``core_so`` is ``None``).
    ``command`` is the raw ES-DE command text, retained so a standalone option
    can be rendered into an ``EmulatorInvocation`` (a libretro option rebuilds
    from ``core_so``); it is not exposed on the frontend payload.

    ``status`` is the bake verdict:

    - ``"bakeable"`` — a real emulator invocation ending in ``%ROM%`` the plugin
      can bake into a shortcut ``-e`` override (``reason`` is ``None``).
    - ``"needs_setup"`` — a command that is well-formed but not yet launchable
      from Steam as-is: a ``%INJECT%`` form that needs ES-DE to generate a
      sidecar first (reason ``"inject"``), or a standalone emulator that is not
      installed in RetroDECK (reason ``"not_installed"``, applied post-hoc by
      :func:`downgrade_if_not_installed` from the adapter's on-disk probe).
    - ``"unbakeable"`` — cannot be baked; ``reason`` says why.

    ``reason`` is ``None`` when bakeable, else one of ``"inject"``,
    ``"not_installed"``, ``"shortcut_script"``, ``"no_rom_target"``,
    ``"quoting"``, ``"startdir"``, ``"unknown_placeholder"``.
    """

    label: str
    kind: str
    core_so: str | None
    command: str
    status: str
    reason: str | None


def classify_command(label: str, text: str) -> EmulatorOption:
    """Classify a single ES-DE ``<command>`` (``label`` + ``text``).

    Applies the bake-verdict rules in order (the first that matches wins) and
    determines the emulator kind, returning a fully-populated
    :class:`EmulatorOption`. Pure — no I/O, deterministic in its inputs.
    """
    status, reason = _bake_verdict(text)
    kind, core_so = _emulator_kind(text)
    return EmulatorOption(
        label=label,
        kind=kind,
        core_so=core_so,
        command=text.strip(),
        status=status,
        reason=reason,
    )


def downgrade_if_not_installed(option: EmulatorOption, emulator_installed: bool) -> EmulatorOption:
    """Downgrade a bakeable **standalone** option whose emulator is not installed.

    The pure half of ADR-0020's binary-existence probe: the adapter performs the
    on-disk / ``es_find_rules.xml`` I/O to decide whether a standalone emulator is
    installed in RetroDECK and passes the verdict in as *emulator_installed*; this
    rule turns a bakeable standalone whose emulator is absent into a
    ``needs_setup`` option with reason ``"not_installed"`` so it drops out of the
    default selection (:func:`select_default_option`) and shows disabled in the
    picker — a system whose only bakeable command is a missing standalone then
    plain-launches, restoring the pre-standalone behavior. Libretro options
    (RetroArch ships with RetroDECK, so it is always installed) and options that
    are already non-bakeable are returned unchanged.
    """
    if emulator_installed or option.status != "bakeable" or option.kind != "standalone":
        return option
    return replace(option, status="needs_setup", reason="not_installed")


def _bake_verdict(text: str) -> tuple[str, str | None]:
    """Return ``(status, reason)`` for a command, first matching rule wins.

    Order matters: an ``%INJECT%`` command is *needs-setup* even though it also
    lacks a ``%ROM%`` target, and a ``.desktop``/OS-shell form is flagged as a
    script before the ``%ROM%``-ending check. ``%STARTDIR%`` is deliberately
    checked *after* the unknown-placeholder sweep (it is a known placeholder)
    so it surfaces its own ``startdir`` reason rather than being lumped in.
    """
    t = text.strip()
    if "%INJECT%" in t:
        return ("needs_setup", "inject")
    if "%ENABLESHORTCUTS%" in t or "%EMULATOR_OS-SHELL%" in t:
        return ("unbakeable", "shortcut_script")
    if not t.endswith("%ROM%"):
        return ("unbakeable", "no_rom_target")
    if '"' in t or "\\;" in t:
        return ("unbakeable", "quoting")
    for token in _PLACEHOLDER_RE.findall(t):
        if token not in _KNOWN_PLACEHOLDERS and not token.startswith("%EMULATOR_"):
            return ("unbakeable", "unknown_placeholder")
    if "%STARTDIR%" in t:
        return ("unbakeable", "startdir")
    return ("bakeable", None)


def _emulator_kind(text: str) -> tuple[str, str | None]:
    """Return ``("libretro", core_so)`` for the RetroArch shape, else ``("standalone", None)``.

    A leading ``env VAR=val …`` prefix (the gc/wii Dolphin form) is a standalone
    invocation — it does not match the strict libretro pattern — and is left as
    ``standalone`` with no core.
    """
    match = _LIBRETRO_RE.match(text.strip())
    if match:
        return ("libretro", match.group(1))
    return ("standalone", None)


def select_default_option(options: list[EmulatorOption]) -> EmulatorOption | None:
    """Return the first *bakeable* option in document order, or ``None``.

    ES-DE lists a system's emulators in preference order; the first one the
    plugin can bake is the system-layer default. Skips ``needs_setup`` and
    ``unbakeable`` options (the ``%INJECT%`` / shortcut / quoting forms). When
    nothing is bakeable the caller bakes the plain RetroDECK launch and lets
    RetroDECK resolve the emulator itself.
    """
    for option in options:
        if option.status == "bakeable":
            return option
    return None


def option_to_invocation(option: EmulatorOption | None) -> EmulatorInvocation | None:
    """Render a *bakeable* option into an :class:`EmulatorInvocation`, else ``None``.

    A libretro option yields a libretro invocation keyed on its ``core_so``; a
    standalone option bakes its full command text verbatim. A ``None`` option,
    or one that is not bakeable, resolves to ``None`` (the plain-launch signal).
    """
    if option is None or option.status != "bakeable":
        return None
    if option.kind == "libretro" and option.core_so:
        return EmulatorInvocation.libretro(option.core_so, option.label)
    if option.kind == "standalone" and option.command:
        return EmulatorInvocation.standalone(option.command, option.label)
    return None


def label_to_invocation(options: list[EmulatorOption], label: str) -> EmulatorInvocation | None:
    """Resolve a picked *label* to its :class:`EmulatorInvocation`, else ``None``.

    Finds the option carrying *label* and renders it via
    :func:`option_to_invocation`. Returns ``None`` when no option matches the
    label OR the matched option is not bakeable — the caller treats both as
    "this pin no longer resolves" and degrades to the next layer.
    """
    for option in options:
        if option.label == label:
            return option_to_invocation(option)
    return None


def options_to_payload(options: list[EmulatorOption]) -> list[dict[str, Any]]:
    """Project options into the frontend emulator-picker payload.

    Each entry is ``{label, kind, core_so, is_default, bakeable, reason}``.
    ``is_default`` marks the single option :func:`select_default_option` picks
    (the first bakeable one); ``bakeable`` is ``True`` only for a fully bakeable
    option (``needs_setup`` reads as ``bakeable: False`` with its ``reason`` —
    ``"inject"`` or ``"not_installed"`` — so the picker can disable it with a
    distinct message). The raw ``command`` text is intentionally dropped from the
    wire payload.
    """
    default = select_default_option(options)
    return [
        {
            "label": option.label,
            "kind": option.kind,
            "core_so": option.core_so,
            "is_default": option is default,
            "bakeable": option.status == "bakeable",
            "reason": option.reason,
        }
        for option in options
    ]
