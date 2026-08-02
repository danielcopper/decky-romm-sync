"""Local generation-gated candidate discovery and paged preview projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from domain.fetch_generation import prune_candidate_ids
from domain.prune import group_rows
from lib.url_host import romm_namespace
from services.prune._models import PrunePreview

_PREVIEW_TEXT_CHARS = 512
_PREVIEW_WARNING_CHARS = 1024
_PREVIEW_BUDGET_BYTES = 48 * 1024

if TYPE_CHECKING:
    from domain.rom import Rom
    from domain.rom_install import RomInstall
    from services.protocols import RecoveryBundleStore, RetroDeckPaths, UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True)
class PreviewBuilderConfig:
    """Dependencies for local-only prune candidate projection."""

    uow_factory: UnitOfWorkFactory
    recovery_store: RecoveryBundleStore
    retrodeck_paths: RetroDeckPaths
    settings: dict[str, Any]


def _explicit_candidate(uow: UnitOfWork, explicit_rom_id: int | None) -> set[int]:
    """The single row an explicit per-ROM cleanup names, if it still exists."""
    if explicit_rom_id is None or not uow.roms.get(explicit_rom_id):
        return set()
    return {explicit_rom_id}


def _discovered_candidates(uow: UnitOfWork, rows: list[Rom]) -> set[int]:
    """Every row absent from its own platform's last completed fetch."""
    platform_rows: dict[str, list[Rom]] = {}
    for row in rows:
        platform_rows.setdefault(row.platform_slug, []).append(row)
    candidate_ids: set[int] = set()
    for slug, candidates in platform_rows.items():
        candidate_ids.update(prune_candidate_ids(candidates, uow.platform_sync_state.get(slug)))
    return candidate_ids


def _preview_entry(
    row: Rom,
    *,
    group_id: str,
    group_size: int,
    bound_count: int,
    candidate: bool,
    install: RomInstall | None,
    size: int | None,
    warning: str | None,
) -> dict[str, Any]:
    """One disclosure row, with every server-supplied string bounded for the wire."""
    return {
        "rom_id": row.rom_id,
        "name": row.name[:_PREVIEW_TEXT_CHARS],
        "name_truncated": len(row.name) > _PREVIEW_TEXT_CHARS,
        "fs_name": row.fs_name[:_PREVIEW_TEXT_CHARS],
        "fs_name_truncated": len(row.fs_name) > _PREVIEW_TEXT_CHARS,
        "platform_slug": row.platform_slug,
        "group_id": group_id[:_PREVIEW_TEXT_CHARS],
        "group_id_truncated": len(group_id) > _PREVIEW_TEXT_CHARS,
        "group_size": group_size,
        "bound_count": bound_count,
        "candidate": candidate,
        "installed": install is not None,
        "installed_bytes": size,
        "warning": warning[:_PREVIEW_WARNING_CHARS] if warning is not None else None,
        "warning_truncated": warning is not None and len(warning) > _PREVIEW_WARNING_CHARS,
    }


class PreviewBuilder:
    """Build immutable candidate snapshots without contacting RomM."""

    def __init__(self, *, config: PreviewBuilderConfig) -> None:
        self._uow_factory = config.uow_factory
        self._recovery_store = config.recovery_store
        self._retrodeck_paths = config.retrodeck_paths
        self._settings = config.settings

    def _installed_size(self, install: RomInstall | None, roms_root: str) -> tuple[int | None, str | None]:
        """Measured bytes of a row's installed content, or why they could not be read."""
        if install is None:
            return None, None
        try:
            return self._recovery_store.measure_path(install.rom_dir or install.file_path, roms_root), None
        except (OSError, ValueError) as exc:
            return None, str(exc)

    def build(self, preview_id: str, scope: Literal["bulk", "rom"], explicit_rom_id: int | None) -> PrunePreview:
        with self._uow_factory() as uow:
            rows = list(uow.roms.iter_all())
            installs = {install.rom_id: install for install in uow.rom_installs.iter_all()}
            candidate_ids = (
                _explicit_candidate(uow, explicit_rom_id) if scope == "rom" else _discovered_candidates(uow, rows)
            )

            relevant_groups = [
                group for group in group_rows(rows) if candidate_ids.intersection(r.rom_id for r in group)
            ]
            fingerprint = self._fingerprint(relevant_groups, installs)

        roms_root = self._retrodeck_paths.roms_path()
        entries: list[dict[str, Any]] = []
        for group in relevant_groups:
            group_id = str(group[0].sibling_group_key or f"rom:{group[0].rom_id}")
            bound_count = sum(row.shortcut_app_id is not None for row in group)
            for row in group:
                install = installs.get(row.rom_id)
                size, warning = self._installed_size(install, roms_root)
                entries.append(
                    _preview_entry(
                        row,
                        group_id=group_id,
                        group_size=len(group),
                        bound_count=bound_count,
                        candidate=row.rom_id in candidate_ids,
                        install=install,
                        size=size,
                        warning=warning,
                    )
                )
        # Candidates first, so the paged list opens on the rows a run can actually
        # remove and the retained siblings read as the subordinate disclosure they
        # are. Within each block the old platform/name/id order still applies.
        entries.sort(
            key=lambda entry: (
                not entry["candidate"],
                str(entry["platform_slug"]),
                str(entry["name"]),
                int(entry["rom_id"]),
            )
        )
        return PrunePreview(
            preview_id=preview_id,
            scope=scope,
            explicit_rom_id=explicit_rom_id,
            candidate_ids=frozenset(candidate_ids),
            fingerprint=fingerprint,
            entries=tuple(entries),
            free_bytes=self._recovery_store.free_bytes(),
            server_namespace=romm_namespace(self._settings),
        )

    def page(self, preview: PrunePreview, offset: int, limit: int) -> dict[str, Any]:
        """Project one byte-bounded window of a snapshot onto the wire.

        ``total`` counts every disclosed row — candidates plus the retained
        siblings a whole-game removal could still take — while
        ``candidate_total`` counts only the rows this run could remove on its
        own. The frontend needs both before it has paged the whole list: the
        headline count must not inflate itself with rows that are merely
        disclosed.
        """
        result: dict[str, Any] = {
            "success": True,
            "preview_id": preview.preview_id,
            "scope": preview.scope,
            "items": [],
            "offset": offset,
            "limit": limit,
            "total": len(preview.entries),
            "candidate_total": sum(1 for entry in preview.entries if entry["candidate"]),
            "free_bytes": self._recovery_store.free_bytes(),
            "recovery_root": self._recovery_store.root(),
        }
        if not limit:
            return result
        items: list[dict[str, Any]] = []
        for entry in preview.entries[offset : offset + limit]:
            candidate = [*items, entry]
            result["items"] = candidate
            if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > _PREVIEW_BUDGET_BYTES:
                if not items:
                    raise ValueError("One cleanup preview entry exceeds the Decky wire budget")
                break
            items = candidate
        result["items"] = items
        return result

    @staticmethod
    def _fingerprint(groups: list[list[Any]], installs: dict[int, Any]) -> tuple[tuple[object, ...], ...]:
        values: list[tuple[object, ...]] = []
        for group in groups:
            for row in group:
                install = installs.get(row.rom_id)
                values.append(
                    (
                        row.rom_id,
                        row.last_fetch_id,
                        row.shortcut_app_id,
                        row.sibling_group_key,
                        install.file_path if install is not None else None,
                        install.rom_dir if install is not None else None,
                    )
                )
        return tuple(sorted(values))
