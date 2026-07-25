from __future__ import annotations

import json
from typing import Any, cast

from services.prune._models import PrunePreview
from services.prune.preview import PreviewBuilder, PreviewBuilderConfig


class _Recovery:
    def free_bytes(self) -> int:
        return 1000

    def root(self) -> str:
        return "/recovery"


def test_preview_pages_stay_within_wire_budget_for_non_ascii_rows() -> None:
    builder = PreviewBuilder(
        config=PreviewBuilderConfig(
            uow_factory=cast("Any", None),
            recovery_store=cast("Any", _Recovery()),
            retrodeck_paths=cast("Any", None),
        )
    )
    entries = tuple(
        {
            "rom_id": index,
            "name": "é" * 512,
            "name_truncated": True,
            "fs_name": "遊" * 512,
            "fs_name_truncated": True,
            "platform_slug": "platform",
            "group_id": "é" * 512,
            "group_id_truncated": True,
            "group_size": 1,
            "bound_count": 0,
            "candidate": True,
            "installed": True,
            "installed_bytes": 1,
            "warning": "遊" * 1024,
            "warning_truncated": True,
        }
        for index in range(1, 51)
    )
    preview = PrunePreview("preview", "bulk", None, frozenset(range(1, 51)), (), entries, 1000)
    offset = 0
    seen: list[int] = []

    while offset < len(entries):
        page = builder.page(preview, offset, 50)
        assert len(json.dumps(page, ensure_ascii=True).encode("utf-8")) <= 48 * 1024
        ids = [item["rom_id"] for item in page["items"]]
        assert ids
        seen.extend(ids)
        offset += len(ids)

    assert seen == list(range(1, 51))
