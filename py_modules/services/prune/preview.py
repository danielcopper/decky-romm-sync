"""Local generation-gated candidate discovery and paged preview projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from domain.fetch_generation import prune_candidate_ids
from domain.prune import group_rows
from services.prune._models import PrunePreview

_PREVIEW_TEXT_CHARS = 512
_PREVIEW_WARNING_CHARS = 1024

if TYPE_CHECKING:
    from services.protocols import RecoveryBundleStore, RetroDeckPaths, UnitOfWorkFactory


@dataclass(frozen=True)
class PreviewBuilderConfig:
    """Dependencies for local-only prune candidate projection."""

    uow_factory: UnitOfWorkFactory
    recovery_store: RecoveryBundleStore
    retrodeck_paths: RetroDeckPaths


class PreviewBuilder:
    """Build immutable candidate snapshots without contacting RomM."""

    def __init__(self, *, config: PreviewBuilderConfig) -> None:
        self._uow_factory = config.uow_factory
        self._recovery_store = config.recovery_store
        self._retrodeck_paths = config.retrodeck_paths

    def build(self, preview_id: str, scope: Literal["bulk", "rom"], explicit_rom_id: int | None) -> PrunePreview:
        with self._uow_factory() as uow:
            rows = list(uow.roms.iter_all())
            installs = {install.rom_id: install for install in uow.rom_installs.iter_all()}
            if scope == "rom":
                candidate_ids = (
                    {explicit_rom_id} if explicit_rom_id is not None and uow.roms.get(explicit_rom_id) else set()
                )
            else:
                candidate_ids: set[int] = set()
                platform_rows: dict[str, list[Any]] = {}
                for row in rows:
                    platform_rows.setdefault(row.platform_slug, []).append(row)
                for slug, candidates in platform_rows.items():
                    candidate_ids.update(prune_candidate_ids(candidates, uow.platform_sync_state.get(slug)))

            relevant_groups = [
                group for group in group_rows(rows) if candidate_ids.intersection(r.rom_id for r in group)
            ]
            fingerprint = self._fingerprint(relevant_groups, installs)

        roms_root = self._retrodeck_paths.roms_path()
        entries: list[dict[str, Any]] = []
        for group in relevant_groups:
            group_id = group[0].sibling_group_key or f"rom:{group[0].rom_id}"
            bound_count = sum(row.shortcut_app_id is not None for row in group)
            for row in group:
                install = installs.get(row.rom_id)
                size: int | None = None
                warning: str | None = None
                if install is not None:
                    source = install.rom_dir or install.file_path
                    try:
                        size = self._recovery_store.measure_path(source, roms_root)
                    except (OSError, ValueError) as exc:
                        warning = str(exc)
                raw_name = row.name
                raw_fs_name = row.fs_name
                raw_group_id = str(group_id)
                entries.append(
                    {
                        "rom_id": row.rom_id,
                        "name": raw_name[:_PREVIEW_TEXT_CHARS],
                        "name_truncated": len(raw_name) > _PREVIEW_TEXT_CHARS,
                        "fs_name": raw_fs_name[:_PREVIEW_TEXT_CHARS],
                        "fs_name_truncated": len(raw_fs_name) > _PREVIEW_TEXT_CHARS,
                        "platform_slug": row.platform_slug,
                        "group_id": raw_group_id[:_PREVIEW_TEXT_CHARS],
                        "group_id_truncated": len(raw_group_id) > _PREVIEW_TEXT_CHARS,
                        "group_size": len(group),
                        "bound_count": bound_count,
                        "candidate": row.rom_id in candidate_ids,
                        "installed": install is not None,
                        "installed_bytes": size,
                        "warning": warning[:_PREVIEW_WARNING_CHARS] if warning is not None else None,
                        "warning_truncated": warning is not None and len(warning) > _PREVIEW_WARNING_CHARS,
                    }
                )
        entries.sort(key=lambda entry: (str(entry["platform_slug"]), str(entry["name"]), int(entry["rom_id"])))
        return PrunePreview(
            preview_id=preview_id,
            scope=scope,
            explicit_rom_id=explicit_rom_id,
            candidate_ids=frozenset(candidate_ids),
            fingerprint=fingerprint,
            entries=tuple(entries),
            free_bytes=self._recovery_store.free_bytes(),
        )

    def page(self, preview: PrunePreview, offset: int, limit: int) -> dict[str, Any]:
        end = offset + limit
        items = list(preview.entries[offset:end]) if limit else []
        return {
            "success": True,
            "preview_id": preview.preview_id,
            "scope": preview.scope,
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": len(preview.entries),
            "free_bytes": self._recovery_store.free_bytes(),
            "recovery_root": None,
        }

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
