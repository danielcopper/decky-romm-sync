"""Records whether a Unit of Work was open when a seam ran.

CONTEXT.md's Unit of Work entry keeps a transaction to database reads and
writes: the real ``SqliteUnitOfWork`` opens with ``BEGIN IMMEDIATE``, SQLite's
global write lock, so a seam doing file or server I/O inside one blocks every
other connection in the plugin until ``busy_timeout`` gives up. A test driving
:class:`FakeUnitOfWork` shares no connection and can never see that lock — the
observable is the ORDER, so this wraps one seam method and records, per call,
whether a unit was open at the moment it ran.

An assertion on the recorded list is non-vacuous both ways: ``[False]`` says the
seam ran once and ran outside, and an empty list — the seam never reached —
fails the same assertion rather than passing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fakes.fake_unit_of_work import FakeUnitOfWork


def record_uow_open(uow: FakeUnitOfWork, seam: object, method: str) -> list[bool]:
    """Wrap ``seam.method`` so every call appends ``uow.is_open`` to the returned list.

    *seam* is the object holding the seam — usually a fake the service under
    test was injected with, but a service patching its own bound seam attribute
    works the same way. The wrapper delegates to the original, so the holder's
    own behavior and recording are unchanged. The returned list grows as the
    service runs — read it after the call under test.
    """
    original: Callable[..., Any] = getattr(seam, method)
    observed: list[bool] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        observed.append(uow.is_open)
        return original(*args, **kwargs)

    setattr(seam, method, recording)
    return observed
