"""Per-ROM plugin cache artifacts that follow a purged ROM aggregate."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from adapters.descriptor_paths import claim_source, remove_claimed
from domain.artwork_paths import cache_filename, cover_meta_filename

if TYPE_CHECKING:
    from models.prune import MutationOutcome, RecoveryArtifact, SourceClaim

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

    def remove(self, rom_ids: list[int], claims: dict[str, SourceClaim] | None = None) -> MutationOutcome:
        changed = False
        ambiguous = False
        for rom_id in rom_ids:
            for path, _kind in self._paths(rom_id):
                try:
                    claim = claims.get(path) if claims is not None else None
                    if claim is None:
                        claim = claim_source(path, self._runtime_dir)
                    outcome = remove_claimed(path, self._runtime_dir, claim)
                except Exception as exc:
                    return {
                        "success": False,
                        "changed": changed,
                        "ambiguous": ambiguous,
                        "message": str(exc),
                    }
                changed |= outcome["changed"]
                ambiguous |= outcome["ambiguous"]
                if not outcome["success"]:
                    return {
                        "success": False,
                        "changed": changed,
                        "ambiguous": ambiguous,
                        "message": outcome["message"],
                    }
        return {"success": True, "changed": changed, "ambiguous": ambiguous, "message": "Artifacts removed"}

    def _paths(self, rom_id: int) -> list[tuple[str, str]]:
        covers = os.path.join(self._runtime_dir, "covers")
        artwork = os.path.join(self._runtime_dir, "artwork")
        paths = [
            (os.path.join(covers, cache_filename(rom_id)), "cover_cache"),
            (os.path.join(covers, cover_meta_filename(rom_id)), "cover_validator"),
        ]
        paths.extend((os.path.join(artwork, f"{rom_id}_{kind}.png"), "sgdb_cache") for kind in _SGDB_TYPES)
        return paths
