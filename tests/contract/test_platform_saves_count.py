"""Contract tests for ``count_platform_saves`` over the real nesting.

The read half of ``delete_platform_saves``, and the number the Library page's
Delete _N_ save files button offers. What this tier pins is that the two agree
over the real installed-ROM rows and the real saves tree: what the count reports
is what the delete removes, and counting removes nothing.

The button disables at zero rather than disappearing, so the answer for a
platform holding nothing has to be a real ``0`` — an absent or refused answer
would leave the button in whatever state the previous platform put it in.
"""

from __future__ import annotations

import os

from ._seed import seed_install


def _write_local_save(harness, *, system: str, filename: str) -> str:
    """Materialize one local save file under the harness saves tree."""
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(b"save")
    return path


async def test_counts_what_the_delete_would_remove(harness):
    seed_install(harness, 1, system="gba", platform_slug="gba", file_name="game1.gba")
    seed_install(harness, 2, system="gba", platform_slug="gba", file_name="game2.gba")
    _write_local_save(harness, system="gba", filename="game1.srm")
    _write_local_save(harness, system="gba", filename="game2.srm")

    result = await harness.plugin.count_platform_saves("gba")

    assert set(result) == {"count"}
    assert result["count"] == 2

    deleted = await harness.plugin.delete_platform_saves("gba")
    assert deleted["deleted_count"] == 2
    assert (await harness.plugin.count_platform_saves("gba"))["count"] == 0


async def test_counting_leaves_the_files_where_they_are(harness):
    seed_install(harness, 1, system="gba", platform_slug="gba", file_name="game1.gba")
    save = _write_local_save(harness, system="gba", filename="game1.srm")

    assert (await harness.plugin.count_platform_saves("gba"))["count"] == 1
    assert (await harness.plugin.count_platform_saves("gba"))["count"] == 1
    assert os.path.exists(save)


async def test_scoped_to_the_platform_asked_about(harness):
    seed_install(harness, 1, system="gba", platform_slug="gba", file_name="game1.gba")
    seed_install(harness, 2, system="snes", platform_slug="snes", file_name="game2.sfc")
    _write_local_save(harness, system="gba", filename="game1.srm")
    _write_local_save(harness, system="snes", filename="game2.srm")

    assert (await harness.plugin.count_platform_saves("gba"))["count"] == 1
    assert (await harness.plugin.count_platform_saves("snes"))["count"] == 1


async def test_a_platform_holding_nothing_answers_zero(harness):
    """Zero, not an absence: the button disables on this answer."""
    seed_install(harness, 1, system="gba", platform_slug="gba", file_name="game1.gba")

    assert (await harness.plugin.count_platform_saves("gba"))["count"] == 0
    assert (await harness.plugin.count_platform_saves("n64"))["count"] == 0
