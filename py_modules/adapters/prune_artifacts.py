"""Per-ROM plugin cache artifacts that follow a purged ROM aggregate."""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

from domain.artwork_paths import cache_filename, cover_meta_filename

if TYPE_CHECKING:
    from models.prune import RecoveryArtifact

_SGDB_TYPES = ("hero", "logo", "grid", "icon")


class PruneArtifactAdapter:
    """Own recovery discovery and deletion for plugin cover/SGDB caches."""

    def __init__(self, *, runtime_dir: str) -> None:
        self._runtime_dir = runtime_dir

    def recovery_artifacts(self, rom_ids: list[int]) -> list[RecoveryArtifact]:
        artifacts: list[RecoveryArtifact] = []
        for rom_id in rom_ids:
            for path, kind in self._paths(rom_id):
                artifacts.append({"source_path": path, "safe_root": self._runtime_dir, "kind": kind, "rom_id": rom_id})
        return artifacts

    def remove(self, rom_ids: list[int]) -> None:
        for rom_id in rom_ids:
            for path, _kind in self._paths(rom_id):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)

    def _paths(self, rom_id: int) -> list[tuple[str, str]]:
        covers = os.path.join(self._runtime_dir, "covers")
        artwork = os.path.join(self._runtime_dir, "artwork")
        paths = [
            (os.path.join(covers, cache_filename(rom_id)), "cover_cache"),
            (os.path.join(covers, cover_meta_filename(rom_id)), "cover_validator"),
        ]
        paths.extend((os.path.join(artwork, f"{rom_id}_{kind}.png"), "sgdb_cache") for kind in _SGDB_TYPES)
        return paths
