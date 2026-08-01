"""In-memory ``RomLaunchPathReader`` implementation for service tests.

Lets the stop-game match be driven without standing up a real
``RelaunchOptionsResolver`` (UoW + install rows + disc resolution): configure
which launch path each rom id resolves to, and every lookup is recorded so a
consumer test can assert the seam was asked at all.
"""

from __future__ import annotations


class FakeRomLaunchPathReader:
    """Returns a configured launch path per rom id.

    ``paths`` maps rom id to the path that ROM's shortcut launches; a rom id
    absent from the map resolves to ``None``, modelling a ROM with no install
    row or no bound shortcut. ``calls`` records the rom ids asked about, in
    order.
    """

    def __init__(self, paths: dict[int, str] | None = None) -> None:
        self.paths: dict[int, str] = dict(paths) if paths else {}
        self.calls: list[int] = []

    def launch_path_for_rom(self, rom_id: int) -> str | None:
        self.calls.append(rom_id)
        return self.paths.get(rom_id)
