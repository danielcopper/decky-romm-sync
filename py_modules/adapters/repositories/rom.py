"""SQLite adapter for the ``Rom`` aggregate over the ``roms`` table."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.rom import Rom

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

# The sync-owned columns: written by the library re-sync UPSERT. Driving
# SELECT/INSERT/VALUES/SET from this ONE tuple keeps them in lockstep so a
# subset omission is impossible (R10). emulator_override and selected_disc are
# deliberately NOT here — they are per-game deviations, not synced identity:
# each is read in SELECT but written only via its own set_*() method, never by
# save(), so a re-sync (which builds a fresh Rom with both = None) cannot wipe a
# user's pin. The version-metadata columns (sibling_group_key + the version
# dimensions) ARE here — they are server-derived facts that must refresh on
# every sync (ADR-0021), the opposite of the user pins. cover_source (the
# cover-cache fingerprint, #1386) rides the UPSERT like cover_path: the commit
# writes the value the artwork layer confirmed this run, else the preserved
# existing one — the merge happens on the Rom before save(), not here.
_SYNC_COLUMNS = (
    "rom_id",
    "platform_slug",
    "name",
    "fs_name",
    "shortcut_app_id",
    "last_synced_at",
    "cover_path",
    "cover_source",
    "igdb_id",
    "sgdb_id",
    "ra_id",
    "sibling_group_key",
    "regions",
    "languages",
    "revision",
    "tags",
    "is_main_sibling",
)

# Read set: the synced columns plus the pin-only emulator_override, selected_disc,
# and applied_launch_options (the recorded applied launch command, #1383). Like the
# two pins, applied_launch_options is read here but written only by its own set_*()
# method, never by save(), so a re-sync never wipes the recorded value.
_SELECT_COLUMNS = ", ".join((*_SYNC_COLUMNS, "emulator_override", "selected_disc", "applied_launch_options"))
_INSERT_COLUMNS = ", ".join(_SYNC_COLUMNS)
_INSERT_PLACEHOLDERS = ", ".join("?" for _ in _SYNC_COLUMNS)
# Every sync column except the rom_id primary key is overwritten on conflict.
_UPDATE_ASSIGNMENTS = ", ".join(f"{col} = excluded.{col}" for col in _SYNC_COLUMNS if col != "rom_id")


def _row_to_rom(row: sqlite3.Row) -> Rom:
    # The JSON-array columns are NOT NULL DEFAULT '[]', so json.loads is always
    # safe; is_main_sibling is a STRICT 0/1 INTEGER mapped back to bool.
    return Rom(
        rom_id=row["rom_id"],
        platform_slug=row["platform_slug"],
        name=row["name"],
        fs_name=row["fs_name"],
        shortcut_app_id=row["shortcut_app_id"],
        last_synced_at=row["last_synced_at"],
        cover_path=row["cover_path"],
        cover_source=row["cover_source"],
        igdb_id=row["igdb_id"],
        sgdb_id=row["sgdb_id"],
        ra_id=row["ra_id"],
        emulator_override=row["emulator_override"],
        selected_disc=row["selected_disc"],
        applied_launch_options=row["applied_launch_options"],
        sibling_group_key=row["sibling_group_key"],
        regions=tuple(json.loads(row["regions"])),
        languages=tuple(json.loads(row["languages"])),
        revision=row["revision"],
        tags=tuple(json.loads(row["tags"])),
        is_main_sibling=bool(row["is_main_sibling"]),
    )


class SqliteRomRepository(BaseRepository):
    """The synced-shortcut registry, one row per tracked ROM."""

    def get(self, rom_id: int) -> Rom | None:
        row = self._conn.execute(f"SELECT {_SELECT_COLUMNS} FROM roms WHERE rom_id = ?", (rom_id,)).fetchone()
        return _row_to_rom(row) if row is not None else None

    def get_by_app_id(self, app_id: int) -> Rom | None:
        # ORDER BY rom_id DESC LIMIT 1: the partial unique index on
        # shortcut_app_id (migration 003) guarantees at most one bound row per
        # appId, so this is single-row in practice. The deterministic order is
        # belt-and-braces for any pre-migration / edge state — it resolves to
        # the newest (MAX rom_id) binding, matching the 003 de-dup's keep-MAX
        # rule, instead of an unspecified scan-order row.
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM roms WHERE shortcut_app_id = ? ORDER BY rom_id DESC LIMIT 1",
            (app_id,),
        ).fetchone()
        return _row_to_rom(row) if row is not None else None

    def save(self, rom: Rom) -> None:
        # Collision-safe bind: a new server-issued rom_id can reuse an old appId
        # (CRC32 of unchanged exe+name) after a server switch / re-import. Unbind
        # any SIBLING row already holding this appId first, so the partial unique
        # index on shortcut_app_id (migration 003) accepts the re-bind. The
        # rom_id != ? guard keeps save() idempotent (never touches the row being
        # upserted, so a same-rom re-save is a no-op here). Unbind-only — the
        # sibling row survives, only its binding is NULLed (ADR-0007), never a
        # DELETE. Skipped when the ROM carries no binding (nothing to collide).
        if rom.shortcut_app_id is not None:
            self._conn.execute(
                "UPDATE roms SET shortcut_app_id = NULL WHERE shortcut_app_id = ? AND rom_id != ?",
                (rom.shortcut_app_id, rom.rom_id),
            )

        # UPSERT (ON CONFLICT … DO UPDATE), never INSERT OR REPLACE: REPLACE
        # deletes-then-inserts the parent row, and that DELETE fires the
        # ON DELETE CASCADE on the per-ROM child tables, silently wiping install,
        # playtime, and save-sync baselines on every re-sync (#887).
        #
        # emulator_override and selected_disc are intentionally absent from both
        # the INSERT column list and the ON CONFLICT SET clause: a re-sync builds
        # a fresh Rom with both = None, and writing either here would wipe a
        # user's pin on every sync. save() owns only the synced-identity columns;
        # each deviation defaults NULL on first insert and is preserved on
        # re-sync (Q1/R10).
        self._conn.execute(
            f"INSERT INTO roms ({_INSERT_COLUMNS}) VALUES ({_INSERT_PLACEHOLDERS}) "
            f"ON CONFLICT(rom_id) DO UPDATE SET {_UPDATE_ASSIGNMENTS}",
            (
                rom.rom_id,
                rom.platform_slug,
                rom.name,
                rom.fs_name,
                rom.shortcut_app_id,
                rom.last_synced_at,
                rom.cover_path,
                rom.cover_source,
                rom.igdb_id,
                rom.sgdb_id,
                rom.ra_id,
                rom.sibling_group_key,
                json.dumps(list(rom.regions)),
                json.dumps(list(rom.languages)),
                rom.revision,
                json.dumps(list(rom.tags)),
                int(rom.is_main_sibling),
            ),
        )

    def set_emulator_override(self, rom_id: int, label: str | None) -> None:
        """Write (or clear) the per-game emulator override for ``rom_id``.

        ``label`` is the core label to pin, or ``None`` to store SQL NULL
        (follow the system default). This is the only write path for the column
        — the sync UPSERT in :meth:`save` never touches it.
        """
        self._conn.execute(
            "UPDATE roms SET emulator_override = ? WHERE rom_id = ?",
            (label, rom_id),
        )

    def get_all_emulator_overrides(self) -> dict[int, str]:
        """Return ``rom_id`` -> pinned core label for every ROM with an override.

        Rows whose ``emulator_override`` is NULL (no override) are omitted, so
        the map holds only the ROMs that deviate from the system default.
        """
        cursor = self._conn.execute("SELECT rom_id, emulator_override FROM roms WHERE emulator_override IS NOT NULL")
        return {row["rom_id"]: row["emulator_override"] for row in cursor}

    def set_selected_disc(self, rom_id: int, filename: str | None) -> None:
        """Write (or clear) the per-game disc selection for ``rom_id``.

        ``filename`` is the disc basename to pin, or ``None`` to store SQL NULL
        (follow the default — the install's ``.m3u`` else disc 1). This is the
        only write path for the column — the sync UPSERT in :meth:`save` never
        touches it.
        """
        self._conn.execute(
            "UPDATE roms SET selected_disc = ? WHERE rom_id = ?",
            (filename, rom_id),
        )

    def set_applied_launch_options(self, rom_id: int, launch_options: str | None) -> None:
        """Record the ``launch_options`` last written to ``rom_id``'s shortcut (#1383).

        ``launch_options`` is the full launch command that was applied (``""`` for
        an uninstalled ROM), or ``None`` to leave it "unknown". The delta apply
        reads this back to skip already-correct shortcuts. This is the only write
        path for the column — the sync UPSERT in :meth:`save` never touches it, so
        a re-sync of an unchanged row (which never re-acks it) preserves the
        recorded value.
        """
        self._conn.execute(
            "UPDATE roms SET applied_launch_options = ? WHERE rom_id = ?",
            (launch_options, rom_id),
        )

    def clear_all_applied_launch_options(self) -> None:
        """Reset every ROM's recorded launch command to "unknown" (NULL).

        The Force Full Sync escape hatch: NULL never matches a target, so the
        next apply re-touches every shortcut instead of delta-skipping it —
        the repair path for Steam-side drift the recorded value cannot see
        (e.g. a manually edited shortcut).
        """
        self._conn.execute("UPDATE roms SET applied_launch_options = NULL")

    def delete(self, rom_id: int) -> None:
        self._conn.execute("DELETE FROM roms WHERE rom_id = ?", (rom_id,))

    def iter_all(self) -> Iterator[Rom]:
        for row in self._conn.execute(f"SELECT {_SELECT_COLUMNS} FROM roms"):
            yield _row_to_rom(row)

    def iter_by_platform(self, platform_slug: str) -> Iterator[Rom]:
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM roms WHERE platform_slug = ?",
            (platform_slug,),
        )
        for row in cursor:
            yield _row_to_rom(row)

    def iter_by_group_key(self, group_key: str) -> Iterator[Rom]:
        # Range-scans the migration-010 index idx_roms_sibling_group_key. A NULL
        # key never matches (WHERE = ? excludes NULL), so an unbackfilled / solo
        # row is correctly absent — the caller treats such a row as its own group.
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM roms WHERE sibling_group_key = ?",
            (group_key,),
        )
        for row in cursor:
            yield _row_to_rom(row)

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0])
