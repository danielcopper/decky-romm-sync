"""Lossless SQLite snapshots and verified recovery-bundle coordination."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from domain.prune import recovery_bundle_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from models.prune import RecoveryArtifact, SteamRecoverySnapshot

    from domain.prune import BundleReadmeContext
    from domain.rom import Rom
    from services.protocols import (
        Clock,
        PruneArtifactStore,
        RecoveryBundleStore,
        RetroDeckPaths,
        SteamRecoveryStore,
        UnitOfWorkFactory,
        UuidGen,
    )


@dataclass(frozen=True)
class RecoveryCoordinatorConfig:
    """Dependencies for pre-cascade state capture and bundle sealing."""

    uow_factory: UnitOfWorkFactory
    recovery_store: RecoveryBundleStore
    prune_artifacts: PruneArtifactStore
    steam_recovery: SteamRecoveryStore
    retrodeck_paths: RetroDeckPaths
    clock: Clock
    uuid_gen: UuidGen


class RecoveryCoordinator:
    """Capture all local aggregate state before a prune can mutate it."""

    def __init__(self, *, config: RecoveryCoordinatorConfig) -> None:
        self._uow_factory = config.uow_factory
        self._recovery_store = config.recovery_store
        self._prune_artifacts = config.prune_artifacts
        self._steam_recovery = config.steam_recovery
        self._retrodeck_paths = config.retrodeck_paths
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen

    def snapshot_state(self, rom_ids: list[int], frontend_steam: dict[str, object] | None) -> dict[str, object]:
        ids = set(rom_ids)
        with self._uow_factory() as uow:
            rows = [row for row in uow.roms.iter_all() if row.rom_id in ids]
            installs = [install for install in uow.rom_installs.iter_all() if install.rom_id in ids]
            metadata = [
                {"rom_id": rom_id, "state": asdict(value)}
                for rom_id, value in uow.rom_metadata.iter_all()
                if rom_id in ids
            ]
            save_sync = [
                {"rom_id": rom_id, "state": asdict(value)}
                for rom_id, value in uow.rom_save_sync_states.iter_all()
                if rom_id in ids
            ]
            playtime = [
                {"rom_id": rom_id, "state": asdict(value)} for rom_id, value in uow.playtime.iter_all() if rom_id in ids
            ]
            platforms = [
                asdict(stamp)
                for slug in sorted({row.platform_slug for row in rows})
                if (stamp := uow.platform_sync_state.get(slug)) is not None
            ]
            collections = [
                asdict(stamp)
                for stamp in uow.collection_sync_state.iter_all()
                if ids.intersection(stamp.member_rom_ids)
            ]
        return {
            "created_at": self._clock.now().isoformat(),
            "roms": [asdict(row) for row in rows],
            "installs": [asdict(install) for install in installs],
            "metadata": metadata,
            "save_sync": save_sync,
            "playtime": playtime,
            "platform_sync_state": platforms,
            "collection_sync_state": collections,
            "steam": frontend_steam,
            "warnings": [],
        }

    def state_matches(
        self,
        expected: dict[str, object],
        rom_ids: list[int],
        committed_action: str | None = None,
        app_id: int | None = None,
        target_id: int | None = None,
        launch_options: str | None = None,
    ) -> bool:
        """Compare all database state represented by a sealed recovery snapshot."""
        current = self.snapshot_state(rom_ids, None)
        keys = (
            "roms",
            "installs",
            "metadata",
            "save_sync",
            "playtime",
            "platform_sync_state",
            "collection_sync_state",
        )
        projected = {key: expected.get(key) for key in keys}
        raw_roms = projected.get("roms")
        if isinstance(raw_roms, list) and committed_action is not None and app_id is not None:
            projected["roms"] = self._project_committed_rows(
                raw_roms, committed_action, app_id, target_id, launch_options
            )
        return all(current.get(key) == projected.get(key) for key in keys)

    @staticmethod
    def _project_committed_rows(
        raw_roms: list[Any],
        committed_action: str,
        app_id: int,
        target_id: int | None,
        launch_options: str | None,
    ) -> list[Any]:
        """Replay an already-committed Steam action onto the sealed rows.

        The database has moved on by exactly that action, so comparing against
        the sealed rows verbatim would report drift the run itself caused.
        """
        roms = [dict(row) if isinstance(row, dict) else row for row in raw_roms]
        for row in roms:
            if not isinstance(row, dict):
                continue
            if row.get("shortcut_app_id") == app_id:
                row["shortcut_app_id"] = None
            if committed_action == "repoint_shortcut" and row.get("rom_id") == target_id:
                row["shortcut_app_id"] = app_id
                row["applied_launch_options"] = launch_options
        return roms

    def seal(
        self,
        *,
        rows: list[Rom],
        snapshot: dict[str, object],
        save_inventory: dict[str, Any],
        include_installed_rom_ids: set[int],
        delete_ids: set[int],
        app_id: int | None,
        should_abort: Callable[[], bool] | None = None,
    ) -> tuple[str, SteamRecoverySnapshot | None]:
        """Seal one group's recovery bundle and report where it landed.

        *should_abort* is handed to the store so a cancelled run stops copying
        within a chunk; an abort raises rather than returning a partial bundle.
        """
        rom_ids = [row.rom_id for row in rows]
        artifacts: list[RecoveryArtifact] = list(save_inventory["artifacts"])
        artifacts.extend(self._prune_artifacts.recovery_artifacts(sorted(delete_ids)))
        roms_root = self._retrodeck_paths.roms_path()
        raw_installs = snapshot.get("installs")
        installs = raw_installs if isinstance(raw_installs, list) else []
        installs_by_id = {
            int(item["rom_id"]): item for item in installs if isinstance(item, dict) and type(item.get("rom_id")) is int
        }
        for rom_id in sorted(include_installed_rom_ids.intersection(rom_ids)):
            install = installs_by_id.get(rom_id)
            if install is None:
                continue
            source = install.get("rom_dir") or install.get("file_path")
            if isinstance(source, str) and source:
                artifacts.append(
                    {"source_path": source, "safe_root": roms_root, "kind": "installed_rom", "rom_id": rom_id}
                )

        steam_backend: SteamRecoverySnapshot | None = None
        if app_id is not None:
            steam_backend = self._steam_recovery.snapshot(app_id)
            steam_artifacts = steam_backend["artifacts"]
            artifacts.extend(steam_artifacts)
            snapshot["steam_backend"] = {
                "user_id": steam_backend["user_id"],
                "user_dir": steam_backend["user_dir"],
                "steam_root": steam_backend["steam_root"],
                "controller_setting": steam_backend["controller_setting"],
                "artifact_count": len(steam_artifacts),
            }
        warnings = snapshot.get("warnings")
        if isinstance(warnings, list):
            warnings.extend(save_inventory["warnings"])
            if save_inventory["shared"]:
                warnings.append("Shared current saves were copied but deliberately left in place")

        now = self._clock.now()
        names = {row.rom_id: row.name for row in rows}
        # Named after the row the run is actually removing, not the lowest id in
        # the group — a folder called after a version that survives would point
        # at the wrong thing.
        headline = min(delete_ids) if delete_ids else min(rom_ids)
        bundle_id = recovery_bundle_id(
            names.get(headline, ""),
            now.strftime("%Y-%m-%d"),
            self._uuid_gen.uuid4().replace("-", "")[:8],
        )
        readme_context: BundleReadmeContext = {
            "bundle_id": bundle_id,
            "created_at": now.isoformat(),
            "games": [
                {
                    "rom_id": row.rom_id,
                    "name": row.name,
                    "fs_name": row.fs_name,
                    "platform_slug": row.platform_slug,
                    "role": self._bundle_role(row.rom_id, delete_ids),
                }
                for row in rows
            ],
            "playtime_lines": self._playtime_lines(snapshot, names),
        }
        if app_id is not None:
            readme_context["steam_app_id"] = app_id
        playtime_text = self._playtime_text(snapshot, names)
        sealed = self._recovery_store.seal_bundle(
            bundle_id, snapshot, artifacts, readme_context, playtime_text, should_abort
        )
        return sealed, steam_backend

    @staticmethod
    def _bundle_role(rom_id: int, delete_ids: set[int]) -> str:
        """Say what this run does to one row, in the reader's terms."""
        return "removed by this cleanup" if rom_id in delete_ids else "kept — recorded for context"

    @staticmethod
    def _playtime_lines(snapshot: dict[str, object], names: dict[int, str]) -> list[str]:
        """Summarise recorded playtime in whole units, named, for the README.

        playtime.txt keeps the exact machine-readable fields; this is the same
        numbers said out loud, because "894" alone tells a person nothing.
        """
        entries = snapshot.get("playtime")
        if not isinstance(entries, list) or not entries:
            return ["No local playtime was recorded for these games."]
        lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rom_id = entry.get("rom_id")
            raw_state = entry.get("state")
            state: dict[str, object] = raw_state if isinstance(raw_state, dict) else {}
            seconds = state.get("total_seconds", 0)
            total = int(seconds) if isinstance(seconds, int) else 0
            name = names.get(rom_id, "unknown game") if isinstance(rom_id, int) else "unknown game"
            lines.append(
                f"{name} (ROM {rom_id}): {total} seconds — {total // 3600}h {total % 3600 // 60}m {total % 60}s"
            )
            pending = state.get("pending_sessions")
            if isinstance(pending, dict) and pending:
                lines.append(f"    plus {len(pending)} session(s) recorded locally but never sent to RomM")
        return lines or ["No local playtime was recorded for these games."]

    @staticmethod
    def _playtime_entry_lines(entry: dict[str, Any], names: dict[int, str] | None) -> list[str]:
        """Write out one ROM's recorded playtime field by field, machine-readable."""
        raw_state = entry.get("state")
        state: dict[str, object] = raw_state if isinstance(raw_state, dict) else {}
        pending_sessions = state.get("pending_sessions")
        pending_count = len(pending_sessions) if isinstance(pending_sessions, dict) else 0
        rom_id = entry.get("rom_id")
        named = (names or {}).get(rom_id) if isinstance(rom_id, int) else None
        lines = [
            f"ROM {rom_id}" + (f" — {named}" if named else ""),
            f"  total_seconds: {state.get('total_seconds', 0)}",
            f"  session_count: {state.get('session_count', 0)}",
            f"  last_played: {state.get('last_played')}",
            f"  last_session_duration_sec: {state.get('last_session_duration_sec')}",
            f"  open_session_start: {state.get('last_session_start')}",
            f"  open_session_monotonic: {state.get('last_session_start_monotonic')}",
            f"  pending_sessions: {pending_count}",
        ]
        if isinstance(pending_sessions, dict):
            lines.extend(RecoveryCoordinator._pending_session_lines(pending_sessions))
        return lines

    @staticmethod
    def _pending_session_lines(pending_sessions: dict[Any, Any]) -> list[str]:
        """Write out every session recorded locally that RomM never accepted."""
        lines: list[str] = []
        for start_time, raw_session in sorted(pending_sessions.items()):
            session = raw_session if isinstance(raw_session, dict) else {}
            lines.extend(
                [
                    f"    start_time: {start_time}",
                    f"      device_id: {session.get('device_id')}",
                    f"      end_time: {session.get('end_time')}",
                    f"      duration_ms: {session.get('duration_ms')}",
                    f"      attempts: {session.get('attempts')}",
                ]
            )
        return lines

    @staticmethod
    def _playtime_text(snapshot: dict[str, object], names: dict[int, str] | None = None) -> str:
        lines = ["Local playtime snapshot", ""]
        entries = snapshot.get("playtime")
        if not isinstance(entries, list) or not entries:
            lines.append("No local playtime state was present.")
            return "\n".join(lines) + "\n"
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lines.extend(RecoveryCoordinator._playtime_entry_lines(entry, names))
        steam = snapshot.get("steam")
        if isinstance(steam, dict):
            lines.extend(
                [
                    "",
                    f"Steam appId: {steam.get('app_id')}",
                    f"Steam lifetime minutes: {steam.get('minutes_playtime_forever')}",
                    f"Steam last-two-weeks minutes: {steam.get('minutes_playtime_last_two_weeks')}",
                ]
            )
        return "\n".join(lines) + "\n"
