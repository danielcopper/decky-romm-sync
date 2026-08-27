"""Read the registry and the completion stamps into the projections the sync's decisions are made against.

Every method here answers a question the sync reasons about before it acts: what
the bound ``roms`` rows looked like before this run (the classify baseline and
the per-unit bound-row projection), which enabled platforms carry no completion
stamp, which sibling-group keys the database already holds, and which bound rows
this run never touched. Nothing here decides anything — the decisions live in
``domain/`` and the moves live in
:class:`~services.library.sync_orchestrator.SyncOrchestrator`. If a method would
not fit the sentence above, it belongs elsewhere.

**The module is declared read-only**, and that is checked:
``scripts/check_read_only_module.py`` fails on any repository call here that is
not a read. The declaration is what makes the boundary worth having — every
method opens its own short Unit of Work and is offloaded through the
orchestrator's executor, so a write slipped in among them would land at a moment
nobody chose.

Two neighbours deliberately stayed with the orchestrator, both touching the same
tables through the same repositories:

* ``_clear_platform_stamp_io`` **writes**, and its correctness is positional —
  after the fetch succeeded, after the artwork, after the cancel guard, before
  the first chunk, so a crash in between leaves no stamp (ADR-0023 / #1025). A
  query can be lifted anywhere because it has no position in the recovery
  protocol; that delete cannot. It and :meth:`RegistryQueries.do_count_unstamped_platforms`
  sit either side of that line: one is a move the caller makes at a chosen
  moment, the other is a question the caller reasons about.
* ``_stamp_component_group_keys`` does no I/O at all — it calls a pure domain
  function and writes the result back onto the caller's own list.

The projections' *shapes* are a contract with :mod:`domain.sync_diff`: an entry
here is what ``classify_roms`` / ``collapse_sibling_groups`` read, and
``services/artwork.py``'s invalidation pass scans the same dicts for
``cover_source``. Changing a key is changing that contract, not this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.sync_diff import select_stale_removals

if TYPE_CHECKING:
    from domain.work_unit import WorkUnit
    from services.protocols import UnitOfWorkFactory


@dataclass(frozen=True)
class RegistryQueriesConfig:
    """Frozen wiring bundle handed to ``RegistryQueries.__init__``.

    Holds the SQLite Unit-of-Work factory — the only dependency the reads have,
    and the point of the grouping: a second one would mean the module answers
    more than one kind of question and the boundary is drawn wrong.
    """

    uow_factory: UnitOfWorkFactory


class RegistryQueries:
    """The registry and completion-stamp reads the sync's decisions are made against."""

    def __init__(self, *, config: RegistryQueriesConfig) -> None:
        self._uow_factory = config.uow_factory

    def do_read_preview_baseline(
        self, slug_to_name: dict[str, str]
    ) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
        """Read the classify baseline from SQLite in one short read UoW.

        Returns ``(registry, last_synced_platforms, last_synced_collections)``
        where ``registry`` is the ``classify_roms``-shaped dict (keyed by
        ``str(rom_id)``) reconstructed from the bound ``roms`` rows, with
        the platform display name resolved from *slug_to_name* (the live
        work-queue) and falling back to the slug. Each entry also carries the
        persisted ``cover_source`` fingerprint so the preview's cover-work
        count (#1386) compares in memory against the same projection — no
        second DB pass. The last-synced platform/collection lists come from
        the newest completed ``SyncRun``.
        """
        with self._uow_factory() as uow:
            registry: dict[str, dict[str, Any]] = {}
            for rom in uow.roms.iter_all():
                if rom.shortcut_app_id is None:
                    continue
                registry[str(rom.rom_id)] = {
                    "app_id": rom.shortcut_app_id,
                    "name": rom.name,
                    "fs_name": rom.fs_name,
                    "platform_name": slug_to_name.get(rom.platform_slug, rom.platform_slug),
                    "platform_slug": rom.platform_slug,
                    "sibling_group_key": rom.sibling_group_key,
                    "applied_launch_options": rom.applied_launch_options,
                    "cover_source": rom.cover_source,
                }
            latest = uow.sync_runs.get_latest_completed()
            last_platforms = list(latest.platforms_completed or []) if latest is not None else []
            last_collections = list(latest.collections_completed or []) if latest is not None else []
        return registry, last_platforms, last_collections

    def do_count_unstamped_platforms(self, platform_slugs: set[str]) -> int:
        """Count enabled platform slugs without a ``PlatformSyncState`` stamp.

        A platform lacking a completion stamp has no wholesale-skip authority —
        ``LibraryFetcher._try_unit_incremental_skip`` full-fetches it — so its
        apply runs even at a zero shortcut delta and the empty final chunk
        re-writes the stamp (the one-time re-walk ADR-0023 intends after a
        late-ack recovery leaves a platform complete-but-unstamped). Surfaced as
        the preview's ``restamp_platform_count`` so the frontend still offers
        Apply on an otherwise-empty delta (#1416). One short read UoW.
        """
        with self._uow_factory() as uow:
            return sum(1 for slug in platform_slugs if uow.platform_sync_state.get(slug) is None)

    def do_read_apply_registry(self, unit: WorkUnit) -> dict[str, dict[str, Any]]:
        """Read the bound-row registry the per-unit group collapse diffs against.

        Platform units scope to their own platform's rows (a sibling group is
        per-platform, so a vanished bound sibling shares the platform); collection
        units read the whole registry since their ROMs span platforms. A platform
        unit's fetch is therefore a COMPLETE view of every group it touches (the
        collapse may rebind); a collection unit's fetch is a PARTIAL view — the
        whole registry surfaces bindings for groups the unit only partly fetched,
        so the collapse must not treat those as vanished (it passes
        ``complete_group_view=False`` and only grandfathers). Only bound rows (a
        live ``shortcut_app_id``) are returned — an unbound sibling is not a
        shortcut the collapse can grandfather or rebind. Each entry also carries
        the persisted ``cover_source`` fingerprint, so the cover-cache
        invalidation pass (#1386) scans against this same projection instead of
        per-ROM DB lookups. The apply path did not read the registry before
        group-aware sync (ADR-0021).
        """
        with self._uow_factory() as uow:
            rows = (
                uow.roms.iter_by_platform(unit.slug) if unit.type == "platform" and unit.slug else uow.roms.iter_all()
            )
            return {
                str(rom.rom_id): {
                    "app_id": rom.shortcut_app_id,
                    "name": rom.name,
                    "fs_name": rom.fs_name,
                    "platform_slug": rom.platform_slug,
                    "sibling_group_key": rom.sibling_group_key,
                    "applied_launch_options": rom.applied_launch_options,
                    "cover_source": rom.cover_source,
                }
                for rom in rows
                if rom.shortcut_app_id is not None
            }

    def do_read_resident_group_keys(self) -> dict[int, str]:
        """Read every persisted non-null ``sibling_group_key`` (``rom_id → key``).

        The preview builds one shortcut set over every enabled platform, so it
        needs the DB's canonical summaries for a fresh member that edges into a
        sibling on a skipped (incremental) platform, which the preview reconstructs
        only its bound rows of. One short read UoW.
        """
        with self._uow_factory() as uow:
            return {
                rom.rom_id: rom.sibling_group_key for rom in uow.roms.iter_all() if rom.sibling_group_key is not None
            }

    def do_scan_stale_roms(self, synced_rom_ids: set[int], synced_app_ids: set[int]) -> list[tuple[int, int]]:
        """Return ``(rom_id, app_id)`` for bound ROMs not synced this run.

        Unbound (stale) rows are skipped — they were already cleared on a
        prior run and carry no Steam shortcut to remove. The ``app_id`` is
        the still-live ``shortcut_app_id`` captured here, before the
        reporter's finalize unbinds the row; the orchestrator threads it
        into the ``sync_stale`` payload so the frontend removes the Steam
        shortcut without re-resolving rom_id→app_id after the unbind.

        Any candidate whose ``app_id`` is in *synced_app_ids* — an appId this
        run bound to a freshly-synced ROM — is excluded by
        :func:`select_stale_removals`: a new server-issued ``rom_id`` can reuse
        an old appId (unchanged ``exe + name``), so the old colliding row looks
        stale but its appId now belongs to the new row. Removing it would wipe
        the shortcut the run just created/updated (#1036).
        """
        with self._uow_factory() as uow:
            candidate_stale = [
                (rom.rom_id, rom.shortcut_app_id)
                for rom in uow.roms.iter_all()
                if rom.shortcut_app_id is not None and rom.rom_id not in synced_rom_ids
            ]
        return select_stale_removals(candidate_stale, synced_app_ids)
