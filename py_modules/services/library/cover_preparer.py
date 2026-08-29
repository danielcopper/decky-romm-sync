"""One work unit's covers, made ready before its shortcuts are emitted.

The **apply path's** cover work for one unit: the invalidation pass that
re-downloads the covers whose server source changed, the download for the
shortcuts the unit is about to emit, and the ``cover_path`` each emitted entry
carries to the frontend. Artwork itself — the per-ROM cover cache, the grid
publish, the conditional revalidation, every byte read or written — belongs to
:class:`~services.artwork.ArtworkService` behind the ``ArtworkManager`` seam,
and stays there: this module supplies what only a run knows (which ROMs a unit
is emitting a shortcut for, where the unit sits in the progress bar, and whether
the run is cancelling) and reads back what only the commit needs (the confirmed
cover fingerprints).

**This is not the run's only artwork holder.** :class:`~services.library.
reporter.SyncReporter` holds the same seam for a different question — turning a
staged cover path into the published grid name at each chunk's commit
(``finalize_cover_path``) — so the package's ``artwork`` confinement names two
owners, and ``scripts/check_seam_owner.py`` enforces exactly that pair. What the
cut settled is narrower and complete: :class:`~services.library.
sync_orchestrator.SyncOrchestrator` holds no ``ArtworkManager`` at all.

**The direction of that split is the reason this is not part of the artwork
service.** The delta vocabulary a cover decision is made in here —
``BIND_ROM_ID_KEY``, an emitted entry, a :class:`~domain.work_unit.WorkUnit` —
is the sync's, and the run signals bound into every call
(``emit_progress``, ``is_cancelling``) are held by the sync's own sub-services.
The artwork service takes both as parameters precisely because it owns neither,
and services do not import one another.

Nothing here opens a transaction or reaches the outside world: the fingerprints
this module returns are persisted by the unit's own commit, one layer up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.sync_diff import BIND_ROM_ID_KEY

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from domain.work_unit import WorkUnit
    from services.library._state import LibrarySyncStateBox
    from services.protocols import ArtworkManager

    EmitProgressFn = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CoverPreparerConfig:
    """Frozen wiring bundle handed to ``CoverPreparer.__init__``.

    Holds the ``ArtworkManager`` peer every cover operation is delegated to, the
    shared :class:`LibrarySyncStateBox` the cancel signal is read from, and the
    orchestrator-supplied ``emit_progress`` callback the cover phases narrate
    through.

    What is absent is the contract: no Unit-of-Work factory, no event loop, no
    settings and no plugin directory — reaching for one would mean this module
    had started doing artwork's own job rather than asking for it.
    """

    artwork: ArtworkManager
    sync_state_box: LibrarySyncStateBox
    emit_progress: EmitProgressFn


class CoverPreparer:
    """Prepares one work unit's covers for the apply that follows."""

    def __init__(self, *, config: CoverPreparerConfig) -> None:
        self._artwork = config.artwork
        self._sync_state = config.sync_state_box
        self._emit_progress = config.emit_progress

    async def attach_unit_cover_paths(
        self,
        unit: WorkUnit,
        unit_roms: list[dict[str, Any]],
        emitted: list[dict[str, Any]],
        *,
        unit_index: int,
        total_units: int,
    ) -> dict[int, str]:
        """Download artwork for the shortcuts about to be emitted and stamp each
        emitted entry's ``cover_path`` in place.

        Only the ROMs that actually get a shortcut (representatives + grandfathered
        siblings) are fetched — no eager covers for versions with no shortcut. A
        rebind entry pulls its cover from the representative it binds
        (``BIND_ROM_ID_KEY``), whose raw dict is the one present in *unit_roms*.
        A no-op when nothing is emitted.

        Returns the confirmed cover fingerprints — ``rom_id → applied cover
        source`` for every ROM whose cache the download resolved (fresh
        download, reuse, or grid seed all confirm; a failed download does not) —
        which the per-unit commit persists as ``roms.cover_source`` (#1386). The
        source is the one ArtworkService *actually* applied: the fresh
        ``path_cover`` normally, or the ROM's ``url_cover`` when the RomM asset
        404s and the external fallback wins (#1450), reported through the
        ``applied_sources`` accumulator.
        """
        if not emitted:
            return {}
        artwork_ids = {int(e.get(BIND_ROM_ID_KEY, e["rom_id"])) for e in emitted}
        artwork_roms = [rom for rom in unit_roms if rom["id"] in artwork_ids]
        applied_sources: dict[int, str] = {}
        cover_paths = await self._download_artwork(
            artwork_roms,
            progress_step=unit_index + 1,
            progress_total_steps=total_units,
            label=unit.name,
            applied_sources=applied_sources,
        )
        for e in emitted:
            e["cover_path"] = cover_paths.get(int(e.get(BIND_ROM_ID_KEY, e["rom_id"])), "")
        return {
            int(rom["id"]): applied_sources.get(int(rom["id"])) or source
            for rom in artwork_roms
            if int(rom["id"]) in cover_paths and (source := rom.get("path_cover_large") or rom.get("path_cover_small"))
        }

    async def _download_artwork(
        self, all_roms, progress_step=4, progress_total_steps=6, label="", applied_sources=None
    ):
        """Delegate artwork download to ArtworkService callback.

        ``label`` is the unit's display name, threaded into the cover-download
        progress frames ("Preparing covers for <label>"). ``applied_sources`` is
        the optional accumulator ArtworkService fills with the cover source
        actually applied per ROM (``url_cover`` on a 404 fallback, #1450), so the
        per-unit commit persists a truthful ``cover_source`` fingerprint.
        """
        box = self._sync_state
        return await self._artwork.download_artwork(
            all_roms,
            emit_progress=self._emit_progress,
            is_cancelling=box.is_cancelling,
            progress_step=progress_step,
            progress_total_steps=progress_total_steps,
            label=label,
            applied_sources=applied_sources,
        )

    async def refresh_changed_covers(self, unit_roms, registry, progress_step=4, progress_total_steps=6, label=""):
        """Delegate the #1386 cover-cache invalidation pass to the ArtworkManager.

        ``registry`` is the unit's bound-row projection (``do_read_apply_registry``)
        the pass compares fingerprints against — the same read the group collapse
        already made. ``label`` is the unit's display name, threaded into the
        throttled "Refreshing covers for <label>" progress frames. Returns the
        refreshed ``{rom_id, app_id}`` list the first apply chunk carries to the
        frontend.
        """
        box = self._sync_state
        return await self._artwork.refresh_changed_covers(
            unit_roms,
            registry,
            emit_progress=self._emit_progress,
            is_cancelling=box.is_cancelling,
            progress_step=progress_step,
            progress_total_steps=progress_total_steps,
            label=label,
        )
