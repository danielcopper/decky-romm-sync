"""In-memory ``RelaunchOptionsReader`` implementation for service tests.

Lets the two relaunch-items consumers (RetroDECK-home migration, startup
launch-options reconcile) inject the relaunch seam without standing up a real
``RelaunchOptionsResolver`` (UoW + active-core + disc resolution). Configure the
list of items the seam returns; each call is recorded so a consumer test can
assert the seam was queried (delegation) and that its returned list flows
through unchanged.
"""

from __future__ import annotations

from typing import Any


class FakeRelaunchOptionsResolver:
    """Returns a configured relaunch-items list for tests.

    ``items`` is the list every ``installed_relaunch_items`` call returns;
    ``calls`` counts the queries so a consumer test can assert the seam was
    reached (the delegation) without re-asserting the deep resolution behavior,
    which the real resolver's own tests own.
    """

    def __init__(self, *, items: list[dict[str, Any]] | None = None) -> None:
        self.items: list[dict[str, Any]] = items if items is not None else []
        self.calls = 0

    def installed_relaunch_items(self) -> list[dict[str, Any]]:
        self.calls += 1
        return self.items
