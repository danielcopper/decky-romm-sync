"""In-memory ``FirmwareCachePersister`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import Any


class FakeFirmwareCachePersister:
    """In-memory ``FirmwareCachePersister`` for tests.

    Keeps the most recently saved dict in ``self.last_saved`` and the
    canned payload returned by ``load`` in ``self.canned_load``. The
    persister contract returns ``dict`` (never ``None``) so the default
    canned load is an empty dict, mirroring the adapter's behaviour
    when no on-disk cache is present.
    """

    def __init__(
        self, *, canned_load: dict[str, Any] | None = None, load_side_effect: BaseException | None = None
    ) -> None:
        self.canned_load: dict[str, Any] = canned_load if canned_load is not None else {}
        self.load_side_effect = load_side_effect
        self.last_saved: dict[str, Any] | None = None
        self.save_count = 0
        self.load_count = 0
        self.save_side_effect: BaseException | None = None

    def save(self, data: dict[str, Any]) -> None:
        self.save_count += 1
        if self.save_side_effect is not None:
            raise self.save_side_effect
        self.last_saved = copy.deepcopy(data)

    def load(self) -> dict[str, Any]:
        self.load_count += 1
        if self.load_side_effect is not None:
            raise self.load_side_effect
        return self.canned_load
