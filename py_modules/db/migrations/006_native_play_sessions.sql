-- =============================================================================
-- 006_native_play_sessions.sql — native play-session outbox; retire the note id
-- Issue #1219 / ADR-0018 (native play-session tracking, additive per-session ingest)
-- =============================================================================
--
-- Playtime moves off the RomM ``romm-sync:playtime`` note onto RomM's native
-- ``/api/play-sessions`` ingest. Two schema changes:
--
--   1. rom_playtime_sessions — the per-ROM pending-session outbox owned by the
--      Playtime aggregate. Each row is one closed session held until it POSTs to
--      the server; success dequeues it. Keyed (rom_id, start_time), matching the
--      server's ``(user_id, device_id, rom_id, start_time)`` dedup so a re-POST
--      is idempotent. FK to roms(rom_id) ON DELETE CASCADE (mirrors the other
--      per-ROM child tables — a full prune cascades the outbox away). ``attempts``
--      counts consecutive per-row ingest ``error`` verdicts; a row is quarantined
--      (dropped, playtime-only loss) once it reaches the service's threshold, so a
--      permanently-rejected session cannot wedge the outbox forever.
--   2. rom_playtime.note_id is dropped — the note storage hack is retired; the
--      column has no remaining reader. Existing server notes are left in place
--      (harmless, ignored), never mass-deleted.
--
-- Transaction-safe DDL only — the runner (adapters/sqlite_migrations.py) wraps
-- BEGIN/COMMIT and stamps PRAGMA user_version = 6. STRICT + DROP COLUMN require
-- SQLite >= 3.35/3.37 (the Steam Deck ships 3.50), matching the 001 baseline.
-- -----------------------------------------------------------------------------
CREATE TABLE rom_playtime_sessions (
    rom_id      INTEGER NOT NULL REFERENCES roms(rom_id) ON DELETE CASCADE,
    start_time  TEXT    NOT NULL,               -- ISO-8601 session start (dedup key with rom_id)
    device_id   TEXT    NOT NULL,               -- server device id this session was recorded on
    end_time    TEXT    NOT NULL,               -- ISO-8601 session end
    duration_ms INTEGER NOT NULL,               -- suspend-adjusted screen-on time (counted seconds * 1000)
    attempts    INTEGER NOT NULL DEFAULT 0,     -- consecutive ingest-error verdicts; quarantined at the threshold
    PRIMARY KEY (rom_id, start_time)
) STRICT;

ALTER TABLE rom_playtime DROP COLUMN note_id;
