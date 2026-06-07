"""In-memory ``ActiveCoreReader`` implementation for service tests.

Lets Phase-5 consumer tests inject a per-ROM active-core seam without standing
up a real ``ActiveCoreResolver`` (UoW + core-info + system resolver). Configure a
default ``(core_so, label)`` and/or per-``rom_id`` overrides; each call is
recorded so a consumer test can assert the seam was queried by ``rom_id``.
"""

from __future__ import annotations


class FakeActiveCoreResolver:
    """Maps ``rom_id`` to a configured ``(core_so, label)`` for tests.

    ``per_rom`` takes precedence; any ``rom_id`` absent from it resolves to
    ``default``. ``calls`` records each queried ``rom_id`` so consumer tests can
    assert the seam was reached with the right ROM.
    """

    def __init__(
        self,
        *,
        default: tuple[str | None, str | None] = (None, None),
        per_rom: dict[int, tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.default = default
        self.per_rom: dict[int, tuple[str | None, str | None]] = per_rom if per_rom is not None else {}
        self.calls: list[int] = []

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]:
        self.calls.append(rom_id)
        return self.per_rom.get(rom_id, self.default)
