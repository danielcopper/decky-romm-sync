"""Tests for ``bin/rom-launcher`` — the Steam shortcut exec wrapper.

The launcher is a pure exec wrapper: Steam hands it the full launch command
(emulator invocation + ROM path) as argv, and it execs exactly that. It owns no
state, no path resolution, and no emulator knowledge. These tests run the real
bash script via ``subprocess`` and assert the exec-wrapper contract: argv is
passed through verbatim (spaces preserved), missing argv is rejected, and the
exec'd command's exit code propagates.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "rom-launcher"


def _run_launcher(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the launcher with the given argv."""
    return subprocess.run(
        ["bash", str(_LAUNCHER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_execs_given_command_and_args() -> None:
    """Happy path: the launcher execs the command it is handed and its args."""
    result = _run_launcher("/usr/bin/printf", "%s", "hello")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "hello"


def test_preserves_multi_word_argument() -> None:
    """A single argv element containing spaces stays one argument across exec."""
    result = _run_launcher("/usr/bin/printf", "%s", "two words")

    assert result.returncode == 0, result.stderr
    # If the space-bearing arg leaked into two argv elements, printf's single
    # ``%s`` would only emit the first word.
    assert result.stdout == "two words"


def test_no_args_exits_with_usage() -> None:
    """Bad path: no argv at all fails with exit 1 and the usage string."""
    result = _run_launcher()

    assert result.returncode == 1
    assert "Usage: rom-launcher <command> [args...]" in result.stderr


def test_propagates_nonzero_exit_code() -> None:
    """Edge: the exec'd command's non-zero exit code propagates unchanged."""
    result = _run_launcher("/bin/sh", "-c", "exit 7")

    assert result.returncode == 7
