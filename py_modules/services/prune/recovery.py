"""Lossless SQLite snapshots and verified recovery-bundle coordination."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from domain.prune import recovery_bundle_id

if TYPE_CHECKING:
    from models.prune import RecoveryArtifact, SteamRecoverySnapshot

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
            roms = [dict(row) if isinstance(row, dict) else row for row in raw_roms]
            for row in roms:
                if not isinstance(row, dict):
                    continue
                if row.get("shortcut_app_id") == app_id:
                    row["shortcut_app_id"] = None
                if committed_action == "repoint_shortcut" and row.get("rom_id") == target_id:
                    row["shortcut_app_id"] = app_id
                    row["applied_launch_options"] = launch_options
            projected["roms"] = roms
        return all(current.get(key) == projected.get(key) for key in keys)

    def seal(
        self,
        *,
        rows: list[Rom],
        snapshot: dict[str, object],
        save_inventory: dict[str, Any],
        include_installed_rom_ids: set[int],
        delete_ids: set[int],
        app_id: int | None,
    ) -> tuple[str, SteamRecoverySnapshot | None]:
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
        bundle_id = recovery_bundle_id(now.strftime("%Y%m%dT%H%M%SZ"), min(rom_ids), self._uuid_gen.uuid4())
        readme = (
            "decky-romm-sync vanished-ROM recovery bundle\n\n"
            "This bundle is a verified pre-deletion snapshot for manual recovery.\n"
            "There is no automatic restore. Steam-assigned appIds and Steam playtime are recorded,\n"
            "but cannot currently be restored to a newly created shortcut. Save states are not included.\n"
            "manifest.json is the lossless authority; checksums.sha256 verifies copied files.\n"
        )
        playtime_text = self._playtime_text(snapshot)
        sealed = self._recovery_store.seal_bundle(bundle_id, snapshot, artifacts, readme, playtime_text)
        return sealed, steam_backend

    @staticmethod
    def _playtime_text(snapshot: dict[str, object]) -> str:
        lines = ["Local playtime snapshot", ""]
        entries = snapshot.get("playtime")
        if not isinstance(entries, list) or not entries:
            lines.append("No local playtime state was present.")
            return "\n".join(lines) + "\n"
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_state = entry.get("state")
            state: dict[str, object] = raw_state if isinstance(raw_state, dict) else {}
            pending_sessions = state.get("pending_sessions")
            pending_count = len(pending_sessions) if isinstance(pending_sessions, dict) else 0
            lines.extend(
                [
                    f"ROM {entry.get('rom_id')}",
                    f"  total_seconds: {state.get('total_seconds', 0)}",
                    f"  session_count: {state.get('session_count', 0)}",
                    f"  last_played: {state.get('last_played')}",
                    f"  last_session_duration_sec: {state.get('last_session_duration_sec')}",
                    f"  open_session_start: {state.get('last_session_start')}",
                    f"  open_session_monotonic: {state.get('last_session_start_monotonic')}",
                    f"  pending_sessions: {pending_count}",
                ]
            )
            if isinstance(pending_sessions, dict):
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
