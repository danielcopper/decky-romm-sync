"""Rom — the library entry for one ROM the plugin tracks locally.

Identity, the Steam-shortcut binding, and the external-service ids the plugin
resolves for a ROM. Created/updated atomically when a ROM is synced from RomM.
``platform_slug`` is a denormalized RomM slug, not a reference to a local
Platform aggregate (none exists — see ADR-0003); the platform's display name is
resolved live from RomM, not carried here.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class Rom:
    """One ROM as the plugin tracks it locally (identity + shortcut binding).

    ``sibling_group_key`` and the version dimensions (``regions`` / ``languages``
    / ``revision`` / ``tags`` / ``is_main_sibling``) are server-derived facts
    RomM supplies per ROM — the sibling group this dump belongs to and how it
    differs from its siblings (region/language/revision variants). They are set
    at construction from the fetch and refreshed on every sync (they ride the
    sync UPSERT, unlike the user-pin ``emulator_override`` / ``selected_disc``);
    no mutation verbs, as no local flow changes them independently of a sync.
    """

    rom_id: int
    platform_slug: str
    name: str
    fs_name: str
    shortcut_app_id: int | None
    last_synced_at: str
    cover_path: str | None = None
    igdb_id: int | None = None
    sgdb_id: int | None = None
    ra_id: int | None = None
    emulator_override: str | None = None
    selected_disc: str | None = None
    applied_launch_options: str | None = None
    sibling_group_key: str | None = None
    regions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    revision: str = ""
    tags: tuple[str, ...] = ()
    is_main_sibling: bool = False

    @classmethod
    def synced(
        cls,
        *,
        rom_id: int,
        platform_slug: str,
        name: str,
        fs_name: str,
        shortcut_app_id: int | None,
        synced_at: str,
        igdb_id: int | None = None,
        sibling_group_key: str | None = None,
        regions: tuple[str, ...] = (),
        languages: tuple[str, ...] = (),
        revision: str = "",
        tags: tuple[str, ...] = (),
        is_main_sibling: bool = False,
    ) -> Rom:
        """Build a Rom synced from RomM at ISO timestamp ``synced_at``.

        ``shortcut_app_id`` is ``None`` for a non-representative sibling — every
        fetched ROM is persisted for identity + version metadata (ADR-0021), but
        only the group's representative carries a Steam-shortcut binding.
        """
        if rom_id <= 0:
            raise ValueError("rom_id must be positive")
        if not platform_slug:
            raise ValueError("platform_slug is required")
        return cls(
            rom_id=rom_id,
            platform_slug=platform_slug,
            name=name,
            fs_name=fs_name,
            shortcut_app_id=shortcut_app_id,
            last_synced_at=synced_at,
            igdb_id=igdb_id,
            sibling_group_key=sibling_group_key,
            regions=regions,
            languages=languages,
            revision=revision,
            tags=tags,
            is_main_sibling=is_main_sibling,
        )

    def update_cover_path(self, path: str) -> None:
        """Record the local cover-art path once artwork has been written."""
        self.cover_path = path

    def unbind_shortcut(self) -> None:
        """Drop the Steam-shortcut binding, keeping the ROM row otherwise intact.

        Auto-stale removal unbinds rather than deletes (ADR-0007): the row and
        its per-ROM children (playtime, saves, metadata) survive; only the
        ``shortcut_app_id`` link is cleared.
        """
        self.shortcut_app_id = None

    def bind_shortcut(self, app_id: int) -> None:
        """Make this ROM its sibling group's active version (ADR-0021 §2).

        Records the group's Steam-shortcut *app_id* on this row. A version switch
        moves the binding here from the previous representative; the repository's
        collision-unbind then clears the old holder so the one-binding-per-appId
        rule (migration 003) holds. The shortcut's name/appId stay sticky — only
        the binding (and the baked ``launch_options``) move. *app_id* must be a
        positive Steam shortcut id.
        """
        if app_id <= 0:
            raise ValueError("app_id must be a positive Steam shortcut id")
        self.shortcut_app_id = app_id

    def assign_sgdb_id(self, sgdb_id: int) -> None:
        """Stamp the resolved SteamGridDB id."""
        self.sgdb_id = sgdb_id

    def assign_ra_id(self, ra_id: int) -> None:
        """Stamp the resolved RetroAchievements id."""
        self.ra_id = ra_id

    def pin_emulator_override(self, label: str) -> None:
        """Pin a per-game emulator/core override to the core *label*.

        Stores the LABEL the user chose (e.g. ``"PCSX ReARMed"``), not a
        resolved ``.so`` — the ``.so`` is resolved live at launch-bake time, so
        the override survives RetroDECK/ES-DE default changes. A blank or
        whitespace-only *label* is meaningless and raises ``ValueError``; clear
        the override with :meth:`clear_emulator_override` instead.
        """
        stripped = label.strip()
        if not stripped:
            raise ValueError("emulator_override label must not be empty")
        self.emulator_override = stripped

    def clear_emulator_override(self) -> None:
        """Drop the per-game override so the ROM follows the system default."""
        self.emulator_override = None

    def pin_selected_disc(self, filename: str) -> None:
        """Pin the multi-disc launch target to the disc named *filename*.

        Stores the disc's basename (the stable selection key), not a resolved
        path — the absolute path is re-derived live at launch-bake time, so the
        pin survives uninstall/reinstall and RetroDECK-home migration. A blank
        or whitespace-only *filename* is meaningless and raises ``ValueError``;
        clear the selection with :meth:`clear_selected_disc` instead.
        """
        stripped = filename.strip()
        if not stripped:
            raise ValueError("selected_disc filename must not be empty")
        self.selected_disc = stripped

    def clear_selected_disc(self) -> None:
        """Drop the disc pin so the ROM follows the default (m3u or disc 1)."""
        self.selected_disc = None

    def record_applied_launch_options(self, launch_options: str) -> None:
        """Record the ``launch_options`` last written to this ROM's Steam shortcut.

        The delta-restricted apply (#1383) compares each item's freshly built
        target ``launch_options`` against this recorded value to decide whether
        the shortcut is already correct (skip) or must be re-applied (changed).
        Written by the five recorded-state writer sites — sync ack-commit,
        download-complete, uninstall (records ``""``), RetroDECK-home migration
        re-resolve, and version switch — each recording the value it just had the
        frontend write onto the shortcut. ``""`` is the uninstalled placeholder,
        not a missing value; ``None`` (never set here) is "unknown" and always
        forces a re-apply. Excluded from the sync UPSERT (persisted only via the
        pin-only ``set_applied_launch_options`` write path) so an unrelated
        re-save never wipes it, mirroring :meth:`pin_emulator_override`."""
        self.applied_launch_options = launch_options
