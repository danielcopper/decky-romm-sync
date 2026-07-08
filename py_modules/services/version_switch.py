"""VersionSwitchService — the version picker's read + write callables (ADR-0021).

Owns the two frontend callables behind the game-detail version picker.
``get_version_list`` reports every version of a sibling group — the local rows
(rich version dimensions) merged with the server's live ``sibling_roms`` view
(slim stubs for versions not yet synced) — with the active / downloaded / default
markers the picker paints. ``switch_version`` moves the group's Steam-shortcut
binding to a chosen sibling (the active version), persisting a server-only target
first when needed.

A version switch is a pure binding move: the shortcut's name and appId stay
sticky (ADR-0021 §2), ``launch_options`` stays the uninstalled placeholder, and
no save state migrates (ADR-0021 §4). Switching a *downloaded* game — with its
cleanup and save guards — is a later slice (#1298); this service rejects a group
that has any install row. The group is resolved by ``sibling_group_key`` over the
migration-010 index; the server view comes from ``get_rom(bound).sibling_roms``.
Server I/O runs outside the Unit of Work (ADR-0006) — read/close, then fetch,
then a short write UoW.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from domain.rom import Rom
from domain.shortcut_data import extract_version_metadata
from domain.sibling_group import compute_sibling_group_key
from domain.sibling_resolution import AUTO_REGION, resolve_group_representative
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        Clock,
        RommRomReader,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class VersionSwitchServiceConfig:
    """Frozen wiring bundle handed to ``VersionSwitchService.__init__``.

    Carries the runtime infrastructure (event loop, logger, clock), the SQLite
    Unit-of-Work factory (to resolve a sibling group and move its binding), the
    RomM ROM reader (the live ``sibling_roms`` view + per-sibling detail), and the
    live settings dict (read for ``preferred_region`` when ranking the default).
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    uow_factory: UnitOfWorkFactory
    romm_api: RommRomReader
    settings: dict[str, Any]


@dataclass(frozen=True)
class _MemberView:
    """A local sibling row projected to what the picker + the default ranking need."""

    rom_id: int
    name: str
    label: str
    regions: list[str]
    languages: list[str]
    revision: str
    tags: list[str]
    is_main_sibling: bool
    installed: bool


@dataclass(frozen=True)
class _LocalGroup:
    """The local view of a bound ROM's sibling group (captured in one read UoW)."""

    bound_rom_id: int
    group_key: str | None
    platform_slug: str
    preferred_region: str
    members: list[_MemberView] = field(default_factory=list)

    @property
    def member_ids(self) -> set[int]:
        return {m.rom_id for m in self.members}


@dataclass(frozen=True)
class _SwitchContext:
    """The read side of a version switch (bound group + target state), one UoW."""

    bound_rom_id: int
    bound_name: str
    group_key: str | None
    platform_slug: str
    installed_any: bool
    target_is_local: bool
    target_group_key: str | None
    target_app_id: int | None
    target_name: str


def _fs_name_no_ext(fs_name: str) -> str:
    """Filename stem for a local row — ``fs_name_no_ext`` is not a DB column."""
    return os.path.splitext(fs_name)[0]


class VersionSwitchService:
    """Version-picker reads (``get_version_list``) and writes (``switch_version``)."""

    def __init__(self, *, config: VersionSwitchServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._uow_factory = config.uow_factory
        self._romm_api = config.romm_api
        self._settings = config.settings

    # ── get_version_list ─────────────────────────────────────────────────

    async def get_version_list(self, app_id: int) -> dict[str, Any]:
        """Report the version picker's state for the group bound to ``app_id``.

        Returns ``{"multi_version": False}`` when the appId is unknown/unbound or
        the group has a single version (the frontend renders no picker). Otherwise
        ``{"multi_version": True, "versions": [...], "server_query_failed": bool}``
        — one entry per version with ``rom_id`` / ``label`` / ``name`` / the
        version dimensions and the ``synced`` / ``installed`` / ``active`` /
        ``is_default`` markers. Local rows always appear; the server's
        ``sibling_roms`` add any not-yet-synced versions (``synced: False``). When
        the server view can't be fetched the local-only list is returned with the
        additive ``server_query_failed: True`` flag (partial-success carve-out) —
        the picker still works over what's synced.
        """
        app_id = int(app_id)
        local = await self._loop.run_in_executor(None, self._read_local_group, app_id)
        if local is None:
            return {"multi_version": False}

        try:
            bound_detail = await self._loop.run_in_executor(None, self._romm_api.get_rom, local.bound_rom_id)
        except Exception as e:  # transport failure degrades to a local-only list
            self._logger.warning(f"Version list: sibling fetch failed for rom {local.bound_rom_id}: {e}")
            return self._build_version_list(local, server_only_stubs=[], detail_by_id={}, server_query_failed=True)

        stubs = bound_detail.get("sibling_roms") or []
        server_only_stubs = [
            s for s in stubs if int(s.get("id", 0)) not in local.member_ids and int(s.get("id", 0)) > 0
        ]
        detail_by_id = await self._fetch_stub_details(server_only_stubs)
        return self._build_version_list(
            local, server_only_stubs=server_only_stubs, detail_by_id=detail_by_id, server_query_failed=False
        )

    async def _fetch_stub_details(self, stubs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Fetch each server-only sibling's full dims concurrently (best-effort).

        The picker's default badge ranks over the WHOLE group, so a not-yet-synced
        sibling needs its ``regions`` / ``revision`` / ``tags`` / ``is_main_sibling``
        to rank correctly — the slim ``sibling_roms`` stub carries none of them.
        Groups are small (RomM caps siblings low), so the extra reads fan out on
        the executor. A failed fetch is skipped: that stub simply ranks with only
        its label, never failing the whole call.
        """
        if not stubs:
            return {}
        results = await asyncio.gather(
            *(self._loop.run_in_executor(None, self._safe_get_rom, int(s["id"])) for s in stubs),
        )
        return {int(d["id"]): d for d in results if d is not None and int(d.get("id", 0)) > 0}

    def _safe_get_rom(self, rom_id: int) -> dict[str, Any] | None:
        """``get_rom`` that swallows transport failures (returns ``None``)."""
        try:
            return self._romm_api.get_rom(rom_id)
        except Exception as e:  # one stub's failure never fails the whole list
            self._logger.warning(f"Version list: sibling detail fetch failed for rom {rom_id}: {e}")
            return None

    def _read_local_group(self, app_id: int) -> _LocalGroup | None:
        """Capture the bound ROM's local sibling group in one short read UoW.

        Returns ``None`` when no ROM is bound to ``app_id`` (no picker). A row with
        a NULL group key (unbackfilled / solo) yields a one-member group — the
        server view can still surface siblings the local library hasn't synced.
        """
        with self._uow_factory() as uow:
            bound = uow.roms.get_by_app_id(app_id)
            if bound is None:
                return None
            group_key = bound.sibling_group_key
            rows = list(uow.roms.iter_by_group_key(group_key)) if group_key else [bound]
            members = [
                _MemberView(
                    rom_id=r.rom_id,
                    name=r.name,
                    label=_fs_name_no_ext(r.fs_name),
                    regions=list(r.regions),
                    languages=list(r.languages),
                    revision=r.revision,
                    tags=list(r.tags),
                    is_main_sibling=r.is_main_sibling,
                    installed=uow.rom_installs.get(r.rom_id) is not None,
                )
                for r in rows
            ]
            return _LocalGroup(
                bound_rom_id=bound.rom_id,
                group_key=group_key,
                platform_slug=bound.platform_slug,
                preferred_region=self._settings.get("preferred_region", AUTO_REGION),
                members=members,
            )

    def _build_version_list(
        self,
        local: _LocalGroup,
        *,
        server_only_stubs: list[dict[str, Any]],
        detail_by_id: dict[int, dict[str, Any]],
        server_query_failed: bool,
    ) -> dict[str, Any]:
        """Merge local rows + server-only stubs into the picker's version list.

        Local rows carry rich dimensions and are ``synced: True``; a server-only
        stub is ``synced: False``, its dimensions filled from a fetched detail when
        one is available (else empty). A single-version group renders no picker.
        """
        entries: list[dict[str, Any]] = [
            {
                "rom_id": m.rom_id,
                "name": m.name,
                "label": m.label,
                "regions": m.regions,
                "languages": m.languages,
                "revision": m.revision,
                "tags": m.tags,
                "synced": True,
                "installed": m.installed,
            }
            for m in local.members
        ]
        for stub in server_only_stubs:
            rom_id = int(stub["id"])
            detail = detail_by_id.get(rom_id)
            meta = extract_version_metadata(detail) if detail is not None else {}
            entries.append(
                {
                    "rom_id": rom_id,
                    "name": (detail.get("name") if detail else None) or stub.get("name") or "",
                    "label": stub.get("fs_name_no_ext") or stub.get("name") or str(rom_id),
                    "regions": list(meta.get("regions") or []),
                    "languages": list(meta.get("languages") or []),
                    "revision": str(meta.get("revision") or ""),
                    "tags": list(meta.get("tags") or []),
                    "synced": False,
                    "installed": False,
                }
            )

        if len(entries) <= 1:
            return {"multi_version": False}

        default_rom_id = self._resolve_default(local, server_only_stubs, detail_by_id)
        for e in entries:
            e["active"] = e["rom_id"] == local.bound_rom_id
            e["is_default"] = e["rom_id"] == default_rom_id
        return {"multi_version": True, "versions": entries, "server_query_failed": server_query_failed}

    def _resolve_default(
        self,
        local: _LocalGroup,
        server_only_stubs: list[dict[str, Any]],
        detail_by_id: dict[int, dict[str, Any]],
    ) -> int | None:
        """The version the resolution chain would pick as the group's default.

        Runs :func:`resolve_group_representative` with EMPTY installed/binding
        filters, so the badge marks the *natural* default — RomM's
        ``is_main_sibling`` else the 1G1R + preferred-region ranking — independent
        of which version is currently bound (marking that would be circular with
        the ``active`` flag). Ranks over the whole group; a server-only stub with
        no fetched detail ranks with only its label. Returns ``None`` if the chain
        somehow can't decide (never, for a non-empty group) so the badge is simply
        absent rather than the call failing.
        """
        ranking: list[dict[str, Any]] = [
            {
                "rom_id": m.rom_id,
                "is_main_sibling": m.is_main_sibling,
                "regions": m.regions,
                "revision": m.revision,
                "tags": m.tags,
                "fs_name_no_ext": m.label,
            }
            for m in local.members
        ]
        for stub in server_only_stubs:
            rom_id = int(stub["id"])
            detail = detail_by_id.get(rom_id)
            meta = extract_version_metadata(detail) if detail is not None else {}
            ranking.append(
                {
                    "rom_id": rom_id,
                    "is_main_sibling": bool(meta.get("is_main_sibling")),
                    "regions": list(meta.get("regions") or []),
                    "revision": str(meta.get("revision") or ""),
                    "tags": list(meta.get("tags") or []),
                    "fs_name_no_ext": stub.get("fs_name_no_ext") or stub.get("name") or "",
                }
            )
        try:
            return resolve_group_representative(
                ranking, installed_rom_ids=set(), bound_rom_ids=set(), preferred_region=local.preferred_region
            )
        except (ValueError, KeyError) as e:
            self._logger.warning(f"Version list: default resolution failed: {e}")
            return None

    # ── switch_version ───────────────────────────────────────────────────

    async def switch_version(self, app_id: int, target_rom_id: int) -> dict[str, Any]:
        """Move the group's active-version binding to ``target_rom_id``.

        A pure binding move (ADR-0021 §2/§4): the target row is bound to the
        group's ``app_id`` and the repository's collision-unbind clears the old
        representative — no Steam call, no name change, no ``launch_options``
        rewrite, no save-state migration. A server-only target is persisted from
        its RomM detail first (server-derived facts). Guards, each canonical
        ``{success, reason, message}``: an unknown appId → ``not_found``; a target
        outside the group → ``not_in_group``; a target bound to a *different*
        shortcut (grandfathered duplicate, ADR-0021 §5) → ``bound_elsewhere``; a
        group with any downloaded member → ``installed`` (switching a downloaded
        game is #1298); a server-only target whose detail the aggregate rejects →
        ``invalid_target``. On success returns ``{success: True, rom_id, rom_name}``.
        """
        app_id = int(app_id)
        target_rom_id = int(target_rom_id)

        ctx = await self._loop.run_in_executor(None, self._read_switch_context, app_id, target_rom_id)
        if ctx is None:
            return {
                "success": False,
                "reason": ErrorCode.NOT_FOUND.value,
                "message": f"No game is bound to shortcut {app_id}",
            }

        if target_rom_id == ctx.bound_rom_id:
            # Already the active version — a harmless no-op, reported as success.
            return {"success": True, "rom_id": ctx.bound_rom_id, "rom_name": ctx.bound_name}

        if ctx.installed_any:
            return {
                "success": False,
                "reason": "installed",
                "message": "Uninstall the game to switch versions.",
            }

        if ctx.target_is_local:
            if ctx.group_key is not None and ctx.target_group_key != ctx.group_key:
                return self._not_in_group(target_rom_id)
            if ctx.target_app_id is not None and ctx.target_app_id != app_id:
                return self._bound_elsewhere(target_rom_id)
            return await self._loop.run_in_executor(None, self._rebind_local, target_rom_id, app_id, ctx.target_name)

        # Target not persisted locally — fetch its detail, validate membership,
        # then persist + bind it (server-derived facts only).
        try:
            target_dict = await self._loop.run_in_executor(None, self._romm_api.get_rom, target_rom_id)
        except Exception as e:  # surfaced as the canonical unreachable failure
            self._logger.warning(f"Version switch: target fetch failed for rom {target_rom_id}: {e}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "RomM server not reachable.",
            }

        if not self._is_sibling(target_dict, ctx.bound_rom_id, ctx.group_key):
            return self._not_in_group(target_rom_id)

        return await self._loop.run_in_executor(None, self._persist_and_bind, target_dict, app_id, ctx.platform_slug)

    def _read_switch_context(self, app_id: int, target_rom_id: int) -> _SwitchContext | None:
        """Capture the bound group + target state for a switch in one read UoW."""
        with self._uow_factory() as uow:
            bound = uow.roms.get_by_app_id(app_id)
            if bound is None:
                return None
            group_key = bound.sibling_group_key
            rows = list(uow.roms.iter_by_group_key(group_key)) if group_key else [bound]
            target_local = uow.roms.get(target_rom_id)
            check_ids = {r.rom_id for r in rows} | {target_rom_id}
            installed_any = any(uow.rom_installs.get(i) is not None for i in check_ids)
            return _SwitchContext(
                bound_rom_id=bound.rom_id,
                bound_name=bound.name,
                group_key=group_key,
                platform_slug=bound.platform_slug,
                installed_any=installed_any,
                target_is_local=target_local is not None,
                target_group_key=target_local.sibling_group_key if target_local is not None else None,
                target_app_id=target_local.shortcut_app_id if target_local is not None else None,
                target_name=target_local.name if target_local is not None else "",
            )

    def _is_sibling(self, target_dict: dict[str, Any], bound_rom_id: int, group_key: str | None) -> bool:
        """True when ``target_dict`` is a sibling of the bound ROM.

        The bound ROM appears in the target's own ``sibling_roms`` (grouping is
        symmetric); a NULL local group key falls back to the computed key so a
        legacy bound row still validates.
        """
        siblings = target_dict.get("sibling_roms") or []
        if any(int(s.get("id", 0)) == bound_rom_id for s in siblings):
            return True
        return group_key is not None and compute_sibling_group_key(target_dict) == group_key

    def _rebind_local(self, target_rom_id: int, app_id: int, target_name: str) -> dict[str, Any]:
        """Move the binding onto an already-persisted local target (short write UoW)."""
        with self._uow_factory() as uow:
            target = uow.roms.get(target_rom_id)
            if target is None:
                # Raced away between read and write — treat as not_in_group.
                return self._not_in_group(target_rom_id)
            target.bind_shortcut(app_id)
            uow.roms.save(target)
        return {"success": True, "rom_id": target_rom_id, "rom_name": target_name}

    def _persist_and_bind(self, target_dict: dict[str, Any], app_id: int, fallback_platform: str) -> dict[str, Any]:
        """Persist a server-only target (server-derived facts) and bind it.

        Builds the ``Rom`` from the RomM detail via the shared version-metadata
        extraction, binds it to ``app_id`` (the repository's collision-unbind
        clears the old representative), and returns the switch success. Siblings
        share a platform, so the bound group's ``platform_slug`` backstops a detail
        that omits it (``Rom.synced`` requires a non-empty slug). A detail that the
        aggregate rejects (bad id, no resolvable slug) fails ``invalid_target`` —
        the target is a sibling, but its server payload can't become a local row.
        """
        meta = extract_version_metadata(target_dict)
        try:
            rom = Rom.synced(
                rom_id=int(target_dict["id"]),
                platform_slug=target_dict.get("platform_slug") or fallback_platform,
                name=target_dict.get("name") or "",
                fs_name=target_dict.get("fs_name") or "",
                shortcut_app_id=None,
                synced_at=self._clock.now().isoformat(),
                igdb_id=target_dict.get("igdb_id"),
                sibling_group_key=meta["sibling_group_key"],
                regions=tuple(meta["regions"]),
                languages=tuple(meta["languages"]),
                revision=meta["revision"],
                tags=tuple(meta["tags"]),
                is_main_sibling=meta["is_main_sibling"],
            )
        except (ValueError, KeyError) as e:
            self._logger.warning(f"Version switch: could not build target row: {e}")
            return self._invalid_target(int(target_dict.get("id", 0)))
        rom.bind_shortcut(app_id)
        with self._uow_factory() as uow:
            uow.roms.save(rom)
        return {"success": True, "rom_id": rom.rom_id, "rom_name": rom.name}

    @staticmethod
    def _not_in_group(target_rom_id: int) -> dict[str, Any]:
        return {
            "success": False,
            "reason": "not_in_group",
            "message": f"Version {target_rom_id} is not part of this game's sibling group.",
        }

    @staticmethod
    def _invalid_target(target_rom_id: int) -> dict[str, Any]:
        return {
            "success": False,
            "reason": "invalid_target",
            "message": f"Version {target_rom_id}'s server details could not be turned into a local version.",
        }

    @staticmethod
    def _bound_elsewhere(target_rom_id: int) -> dict[str, Any]:
        return {
            "success": False,
            "reason": "bound_elsewhere",
            "message": f"Version {target_rom_id} is already used by another shortcut.",
        }
