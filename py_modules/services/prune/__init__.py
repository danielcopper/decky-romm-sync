"""Explicit, recovery-backed cleanup of local ROM entries absent from RomM."""

from services.prune.service import PruneService, PruneServiceConfig

__all__ = ["PruneService", "PruneServiceConfig"]
