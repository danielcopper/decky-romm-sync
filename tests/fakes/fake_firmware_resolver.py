"""In-memory ``FirmwareResolver`` implementation for service tests."""

from __future__ import annotations

from domain.firmware_wants import FirmwareCatalogue, FirmwarePlacement, FirmwareWant


class FakeFirmwareResolver:
    """The machine's firmware demand, stated by the test instead of read off disk.

    Seed it with :meth:`declare` — one call per file, naming the cores that
    require it and the cores that merely accept it. ``unread_cores`` names the
    emulators whose declaration could not be read, which is what decides whether
    a file the catalogue does not hold reads ``not_needed`` or ``unknown`` for a
    given platform; ``resolved=False`` stands for a reading that never happened
    at all.

    ``calls`` counts invocations so a test can pin that a whole-machine question
    costing hundreds of milliseconds on a real device is asked once per query
    rather than once per platform.
    """

    def __init__(
        self,
        *,
        placements: list[FirmwarePlacement] | None = None,
        unread_cores: frozenset[str] = frozenset(),
        resolved: bool = True,
        caveats: tuple[str, ...] = (),
    ) -> None:
        self.placements: list[FirmwarePlacement] = list(placements or [])
        self.unread_cores = unread_cores
        self.resolved = resolved
        self.caveats = caveats
        self.calls = 0

    def declare(
        self,
        file_name: str,
        *,
        required_by: tuple[str, ...] | list[str] = (),
        optional_for: tuple[str, ...] | list[str] = (),
        relative_path: str | None = None,
        description: str | None = None,
    ) -> FirmwarePlacement:
        """State that some emulators ask for *file_name*, and return the placement.

        ``relative_path`` defaults to the bare file name — the flat layout — so a
        test only spells it out when the subdirectory placement is the point.
        """
        placement = FirmwarePlacement(
            file_name=file_name,
            relative_path=relative_path if relative_path is not None else file_name,
            description=description if description is not None else file_name,
            wants=tuple(
                [FirmwareWant(core_so=core, required=True) for core in required_by]
                + [FirmwareWant(core_so=core, required=False) for core in optional_for]
            ),
        )
        self.placements.append(placement)
        return placement

    def __call__(self) -> FirmwareCatalogue:
        self.calls += 1
        return FirmwareCatalogue(
            placements=tuple(self.placements),
            unread_cores=self.unread_cores,
            resolved=self.resolved,
            caveats=self.caveats,
        )
