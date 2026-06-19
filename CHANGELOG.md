# Changelog

## [0.22.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.21.0...decky-romm-sync-v0.22.0) (2026-06-19)


### Features

* **saves:** detect & gate save sync when savefiles_in_content_dir is on ([#239](https://github.com/danielcopper/decky-romm-sync/issues/239)) ([#963](https://github.com/danielcopper/decky-romm-sync/issues/963)) ([8ac2de2](https://github.com/danielcopper/decky-romm-sync/commit/8ac2de29a93da87e4aed5179a077e660c4b54dfe))


### Bug Fixes

* **connection:** bind stored token to its minting server origin ([#1015](https://github.com/danielcopper/decky-romm-sync/issues/1015), [#1038](https://github.com/danielcopper/decky-romm-sync/issues/1038), [#1039](https://github.com/danielcopper/decky-romm-sync/issues/1039)) ([#1089](https://github.com/danielcopper/decky-romm-sync/issues/1089)) ([a94e3d9](https://github.com/danielcopper/decky-romm-sync/commit/a94e3d93b80dfa0f8840cd2cb45fa65c6419d7a7))
* **migration:** fail loud when the migration_blocked gate is unwired ([#970](https://github.com/danielcopper/decky-romm-sync/issues/970)) ([#1093](https://github.com/danielcopper/decky-romm-sync/issues/1093)) ([aba54ab](https://github.com/danielcopper/decky-romm-sync/commit/aba54ab329f31cd6f0b2513e775bba40457ee663))
* **persistence:** BEGIN IMMEDIATE in the UoW to avoid un-retried SQLITE_BUSY_SNAPSHOT ([#1011](https://github.com/danielcopper/decky-romm-sync/issues/1011)) ([#1092](https://github.com/danielcopper/decky-romm-sync/issues/1092)) ([3674ef6](https://github.com/danielcopper/decky-romm-sync/commit/3674ef6f3b291252ae2b1ba5f3175b325b7ed3c6))
* **persistence:** crash-safe settings writes + corrupt-file quarantine ([#1010](https://github.com/danielcopper/decky-romm-sync/issues/1010)) ([#1090](https://github.com/danielcopper/decky-romm-sync/issues/1090)) ([d794705](https://github.com/danielcopper/decky-romm-sync/commit/d7947052d9fb5c4773e8ef742fe6b81ba3d9367d))
* **persistence:** surface settings-reset as a persistent acknowledgeable notice ([#1091](https://github.com/danielcopper/decky-romm-sync/issues/1091)) ([99eadb1](https://github.com/danielcopper/decky-romm-sync/commit/99eadb1957fb23b4918a172e813b156867b91b65))
* **saves:** adopt identical server save instead of POSTing a duplicate ([#1013](https://github.com/danielcopper/decky-romm-sync/issues/1013)) ([#1099](https://github.com/danielcopper/decky-romm-sync/issues/1099)) ([5e7dd62](https://github.com/danielcopper/decky-romm-sync/commit/5e7dd62cbb2b4a46e06cf76fc3dea835401c321a))
* **saves:** branch-6 conflicts on baseline divergence instead of silent download ([#1059](https://github.com/danielcopper/decky-romm-sync/issues/1059)) ([#1095](https://github.com/danielcopper/decky-romm-sync/issues/1095)) ([06652d1](https://github.com/danielcopper/decky-romm-sync/commit/06652d132c50d818b72f522ead0d244606ed99fc))
* **saves:** group the matrix local-file loop by canonical target ([#1006](https://github.com/danielcopper/decky-romm-sync/issues/1006)) ([#1096](https://github.com/danielcopper/decky-romm-sync/issues/1096)) ([d33ba7f](https://github.com/danielcopper/decky-romm-sync/commit/d33ba7f818553d9d97989eac0b733554dbc184a1))
* **saves:** hold the per-ROM sync lock across slot mutations ([#1057](https://github.com/danielcopper/decky-romm-sync/issues/1057)) ([#1100](https://github.com/danielcopper/decky-romm-sync/issues/1100)) ([d01a3fd](https://github.com/danielcopper/decky-romm-sync/commit/d01a3fd5f91bbb7df65a4acd482bece95a2e5a39))
* **saves:** legacy-slot wire contract — address slot:null saves + explicit confirm_slot_choice ([#1061](https://github.com/danielcopper/decky-romm-sync/issues/1061), [#1008](https://github.com/danielcopper/decky-romm-sync/issues/1008), [#1004](https://github.com/danielcopper/decky-romm-sync/issues/1004), [#1005](https://github.com/danielcopper/decky-romm-sync/issues/1005)) ([#1102](https://github.com/danielcopper/decky-romm-sync/issues/1102)) ([3e1864d](https://github.com/danielcopper/decky-romm-sync/commit/3e1864d3ae1dc0fd7ab577bc0f555be1ffd58c7c))
* **saves:** make switch_slot file handling coherent and backup-safe ([#1058](https://github.com/danielcopper/decky-romm-sync/issues/1058), [#965](https://github.com/danielcopper/decky-romm-sync/issues/965)) ([#1101](https://github.com/danielcopper/decky-romm-sync/issues/1101)) ([24d93a9](https://github.com/danielcopper/decky-romm-sync/commit/24d93a9a8f0bee1b0c3f44171843ebc410044153))
* **security:** reject path traversal in firmware/download joins via lib safe_join ([#1081](https://github.com/danielcopper/decky-romm-sync/issues/1081)) ([48e8839](https://github.com/danielcopper/decky-romm-sync/commit/48e883989d24f6859c532d42a1032fba335f9cae)), closes [#966](https://github.com/danielcopper/decky-romm-sync/issues/966) [#967](https://github.com/danielcopper/decky-romm-sync/issues/967) [#968](https://github.com/danielcopper/decky-romm-sync/issues/968)
* **shortcuts:** escape quotes in launch_options path to block argv injection ([#1084](https://github.com/danielcopper/decky-romm-sync/issues/1084)) ([16770f5](https://github.com/danielcopper/decky-romm-sync/commit/16770f551ded84c4fd94d824de76eafab4d4d45d)), closes [#969](https://github.com/danielcopper/decky-romm-sync/issues/969)
* **ui:** per-game BIOS panel ignores bios change-events for other platforms ([#1083](https://github.com/danielcopper/decky-romm-sync/issues/1083)) ([b44c6c7](https://github.com/danielcopper/decky-romm-sync/commit/b44c6c798a82c8a9deb34e85fdf0d148c208d353)), closes [#1082](https://github.com/danielcopper/decky-romm-sync/issues/1082)

## [0.21.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.20.0...decky-romm-sync-v0.21.0) (2026-06-08)


### ⚠ BREAKING CHANGES

* **downloads:** multi-file ROM downloads now extract into "<launch-file>/" (e.g. "Game.m3u/") instead of "<name>/". Multi-disc and single-disc bin/cue games installed before this version keep their old folder layout in ES-DE until re-downloaded; the plugin's own launch is unaffected. Lazy on-access healing for existing installs is tracked in #951.

### Features

* **cores:** per-game emulator/core override via RetroDECK -e + plugin DB ([#949](https://github.com/danielcopper/decky-romm-sync/issues/949)) ([968df1d](https://github.com/danielcopper/decky-romm-sync/commit/968df1df44e36d7c9a4ce0fda42e84a8ca805671))
* **cores:** plugin owns emulator selection — per-platform core in settings.json, always -e, drop gamelist ([#953](https://github.com/danielcopper/decky-romm-sync/issues/953)) ([384ec5e](https://github.com/danielcopper/decky-romm-sync/commit/384ec5eaf4241c2d19cad4d224a131b88c0fb6eb))
* **downloads:** name multi-file ROM folders after a game playlist so ES-DE collapses them ([#952](https://github.com/danielcopper/decky-romm-sync/issues/952)) ([d55ed09](https://github.com/danielcopper/decky-romm-sync/commit/d55ed0945d1c63f37e37d762a2db115265469068))
* **ui:** add per-platform BIOS delete to the System page ([#934](https://github.com/danielcopper/decky-romm-sync/issues/934)) ([73f2983](https://github.com/danielcopper/decky-romm-sync/commit/73f29835e41c3e51955dfbfedd7adb4eefb7e641))
* **ui:** highlight the active core in the game-page BIOS list ([#955](https://github.com/danielcopper/decky-romm-sync/issues/955)) ([#959](https://github.com/danielcopper/decky-romm-sync/issues/959)) ([fad88b1](https://github.com/danielcopper/decky-romm-sync/commit/fad88b17a4e083fc935794e824cfd0f41b541aa6))
* **ui:** per-game core menu — (system) marker + 'Use System Override' reset item ([#958](https://github.com/danielcopper/decky-romm-sync/issues/958)) ([973672e](https://github.com/danielcopper/decky-romm-sync/commit/973672e6ac74a3de5d49f9ac9f522aea3cbfbd3f))


### Bug Fixes

* **auth:** drop empty Bearer header that deadlocks fresh setup ([#950](https://github.com/danielcopper/decky-romm-sync/issues/950)) ([27f6bae](https://github.com/danielcopper/decky-romm-sync/commit/27f6bae99e82a9de0ec9d952b6cd13590eaa81db)), closes [#928](https://github.com/danielcopper/decky-romm-sync/issues/928)
* **cores:** normalize RomM slug → RetroDECK system before core/gamelist seams ([#919](https://github.com/danielcopper/decky-romm-sync/issues/919)) ([0e27f34](https://github.com/danielcopper/decky-romm-sync/commit/0e27f34fe377cf70d738bceeaaa9bd89eaf2e4d6)), closes [#906](https://github.com/danielcopper/decky-romm-sync/issues/906)
* **cores:** surface active-core fields on no-BIOS platforms so the per-game core menu renders ([#927](https://github.com/danielcopper/decky-romm-sync/issues/927)) ([9da0926](https://github.com/danielcopper/decky-romm-sync/commit/9da0926f31c1f3055231f8e8908dc472cdbc58ab))
* **firmware:** read asdict bios files by key so per-platform BIOS delete works ([#931](https://github.com/danielcopper/decky-romm-sync/issues/931)) ([691c0e7](https://github.com/danielcopper/decky-romm-sync/commit/691c0e76ee3400f29c7b195d2f106e03fefbae15))
* **library:** heartbeat the shortcut scan and scan once per run ([#946](https://github.com/danielcopper/decky-romm-sync/issues/946)) ([8010224](https://github.com/danielcopper/decky-romm-sync/commit/801022486a033bdc41dd3dab94674052f3574daa)), closes [#930](https://github.com/danielcopper/decky-romm-sync/issues/930)
* **paths:** surface RetroDECK config health instead of silent stale-root fallback ([#957](https://github.com/danielcopper/decky-romm-sync/issues/957)) ([51c07a1](https://github.com/danielcopper/decky-romm-sync/commit/51c07a1aeb9e82ee989a97b71373146372dee645))
* **ui:** show the per-game core-switch warning only when the filename triggers it ([#932](https://github.com/danielcopper/decky-romm-sync/issues/932)) ([ba16d46](https://github.com/danielcopper/decky-romm-sync/commit/ba16d463d8ba958a0548ad680e4e687e40eb424d))
* **ui:** single save-compatibility banner + emit bios event on BIOS delete ([#940](https://github.com/danielcopper/decky-romm-sync/issues/940)) ([69e2b1d](https://github.com/danielcopper/decky-romm-sync/commit/69e2b1d2ecacab01ac83c47205deaecad70b9096))
* **ui:** System page lists only currently-synced systems ([#956](https://github.com/danielcopper/decky-romm-sync/issues/956)) ([#960](https://github.com/danielcopper/decky-romm-sync/issues/960)) ([8f3ec42](https://github.com/danielcopper/decky-romm-sync/commit/8f3ec42f0f72835bf56a6cd165bba7b9a0e538ad))
* **ui:** thread rom_filename so the game-detail reads the per-game active core ([#937](https://github.com/danielcopper/decky-romm-sync/issues/937)) ([09de015](https://github.com/danielcopper/decky-romm-sync/commit/09de01586dbda5f443c835bd15cd23b79aa1ccfe))

## [0.20.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.19.0...decky-romm-sync-v0.20.0) (2026-06-05)


### ⚠ BREAKING CHANGES

The JSON→SQLite migration and the rebuilt ROM launcher both require a re-sync
after updating, and old Steam shortcuts must be removed. **Please read before
upgrading.**

**Your data is safe:** downloaded ROM files and on-disk save files are untouched,
and nothing on the RomM server changes. Only the plugin's local tracking state
resets and the Steam shortcuts are recreated (the launcher's path changed, so
every shortcut gets a new app ID).

**Upgrade steps — in order:**

1. **If you use save sync:** in the QAM, open **Settings** and press **Sync All
   Saves Now**, so your latest saves are on RomM before the local state resets.
2. In the QAM, open **Data Management** and press **Remove All RomM Shortcuts** —
   do this on the *current* version, before upgrading, so the old shortcuts are
   cleaned up properly.
3. Upgrade the plugin, then open **Settings** and confirm your configuration is
   correct.
4. Re-sync your library from RomM. This recreates the shortcuts and rebuilds the
   plugin's tracking state; save-sync baselines re-establish on this sync.

**Playtime** is restored automatically — opening a game's detail page pulls its
total back from RomM (per game, on first view). Not restored: per-game session
counts and "last played" timestamps, and any playtime that was never synced to
RomM.

The old JSON files (`state.json`, `metadata_cache.json`, `firmware_cache.json`,
`save_sync_state.json`) are silently ignored and can be deleted by hand from the
plugin's data directory.

### Features

* **auth:** add RomM Client API Token authentication ([#849](https://github.com/danielcopper/decky-romm-sync/issues/849)) ([67a2e96](https://github.com/danielcopper/decky-romm-sync/commit/67a2e96fdcc1179533694452096be35cad83d122)), closes [#163](https://github.com/danielcopper/decky-romm-sync/issues/163)
* **domain:** add mid-tier aggregates for [#788](https://github.com/danielcopper/decky-romm-sync/issues/788) ([#816](https://github.com/danielcopper/decky-romm-sync/issues/816)) ([8f9e2bc](https://github.com/danielcopper/decky-romm-sync/commit/8f9e2bc4af13dc18e6b3adc99b441855566b3803))
* **domain:** add RomSaveState and SyncRun aggregates for [#788](https://github.com/danielcopper/decky-romm-sync/issues/788) ([#817](https://github.com/danielcopper/decky-romm-sync/issues/817)) ([3b563ce](https://github.com/danielcopper/decky-romm-sync/commit/3b563ce6e3b999a648b82e6a6d33a96470b1c5f7))
* **domain:** aggregate enforcement infrastructure for [#788](https://github.com/danielcopper/decky-romm-sync/issues/788) ([#814](https://github.com/danielcopper/decky-romm-sync/issues/814)) ([53a4868](https://github.com/danielcopper/decky-romm-sync/commit/53a4868184a5240ff611d930b90a65c483e90fe6))
* **domain:** simple aggregates (Device, SyncSettings, Playtime) for [#788](https://github.com/danielcopper/decky-romm-sync/issues/788) ([#815](https://github.com/danielcopper/decky-romm-sync/issues/815)) ([cbc4fc0](https://github.com/danielcopper/decky-romm-sync/commit/cbc4fc0052b95063d732376a7b1c6cda6e90271c))
* **persistence:** add SQLite migration framework (user_version) for [#781](https://github.com/danielcopper/decky-romm-sync/issues/781) ([#819](https://github.com/danielcopper/decky-romm-sync/issues/819)) ([84b5f3d](https://github.com/danielcopper/decky-romm-sync/commit/84b5f3dd28e3e51de742e9eb929223f3fa103dbc))
* **persistence:** add SQLite repository adapters + sync Unit of Work ([#826](https://github.com/danielcopper/decky-romm-sync/issues/826)) ([a8334e2](https://github.com/danielcopper/decky-romm-sync/commit/a8334e2760bc9ab32b8427156b8427accc023d84)), closes [#783](https://github.com/danielcopper/decky-romm-sync/issues/783)
* **persistence:** define Repository Protocols ([#782](https://github.com/danielcopper/decky-romm-sync/issues/782)) ([#825](https://github.com/danielcopper/decky-romm-sync/issues/825)) ([3955af6](https://github.com/danielcopper/decky-romm-sync/commit/3955af6ce2a6da27a1c0db2a0bf94bd01ea72b92))
* **persistence:** SQLite schema DDL + table layout for [#780](https://github.com/danielcopper/decky-romm-sync/issues/780) ([#818](https://github.com/danielcopper/decky-romm-sync/issues/818)) ([a9fea00](https://github.com/danielcopper/decky-romm-sync/commit/a9fea008c802cac5553c5c310d935177cc754a2b))
* **playtime:** reconcile playtime from RomM notes on game-detail open ([#905](https://github.com/danielcopper/decky-romm-sync/issues/905)) ([2e25eaf](https://github.com/danielcopper/decky-romm-sync/commit/2e25eaf08502c87d6aa8688364c31dc89aef7afa))
* **saves:** single-token memory-card extensions, keyed by RetroDECK system ([#904](https://github.com/danielcopper/decky-romm-sync/issues/904)) ([040defd](https://github.com/danielcopper/decky-romm-sync/commit/040defd6a932a3ccfd259675562c43dcf62a6820))


### Bug Fixes

* **ci:** skip docs-check on release-please file changes ([#800](https://github.com/danielcopper/decky-romm-sync/issues/800)) ([451217d](https://github.com/danielcopper/decky-romm-sync/commit/451217d2dafd763a57925de9da080abcff7e74b9))
* **docs:** exclude adr/ from published MkDocs site ([#809](https://github.com/danielcopper/decky-romm-sync/issues/809)) ([ad5c510](https://github.com/danielcopper/decky-romm-sync/commit/ad5c5105fc8ac2b838e3c814d236557d010af93e))
* **downloads:** key multi-file detection on total file count, not has_multiple_files ([#857](https://github.com/danielcopper/decky-romm-sync/issues/857)) ([c49aac6](https://github.com/danielcopper/decky-romm-sync/commit/c49aac68fb4b605a05247560223d6a302bfbf1fe)), closes [#855](https://github.com/danielcopper/decky-romm-sync/issues/855) [#837](https://github.com/danielcopper/decky-romm-sync/issues/837)
* **persistence:** upsert roms registry so re-sync keeps per-ROM children ([#888](https://github.com/danielcopper/decky-romm-sync/issues/888)) ([5b65fde](https://github.com/danielcopper/decky-romm-sync/commit/5b65fde7947e01598e14db2f6a3b359d43d6b16b)), closes [#887](https://github.com/danielcopper/decky-romm-sync/issues/887)
* **saves:** allow null tracked_save_id for hash-only baselines ([#873](https://github.com/danielcopper/decky-romm-sync/issues/873)) ([7de822a](https://github.com/danielcopper/decky-romm-sync/commit/7de822af6fcb5bef64ebbcabdf8159d2a6e01f4d))
* **saves:** register device with /etc/machine-id fingerprint ([#880](https://github.com/danielcopper/decky-romm-sync/issues/880)) ([494f73b](https://github.com/danielcopper/decky-romm-sync/commit/494f73be9ae274b5786f534b29e17ded738c5442))
* **saves:** serialize StatusService save-status RMW under rom_lock ([#874](https://github.com/danielcopper/decky-romm-sync/issues/874)) ([86d4fc7](https://github.com/danielcopper/decky-romm-sync/commit/86d4fc7e349737e4ed747e0d4930ff8afe0efe71))
* **types:** make @decky/ui + callable boundary types honest ([#858](https://github.com/danielcopper/decky-romm-sync/issues/858)) ([9f1e270](https://github.com/danielcopper/decky-romm-sync/commit/9f1e270561188e4d5a1951a5898985b5ceb59093))


### Miscellaneous Chores

* **persistence:** flag JSON→SQLite cutover as a breaking upgrade ([#913](https://github.com/danielcopper/decky-romm-sync/issues/913)) ([07e0665](https://github.com/danielcopper/decky-romm-sync/commit/07e06659f9ca5c95491591bb7ba9c6a8a6dc99ba))

## [0.19.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.18.0...decky-romm-sync-v0.19.0) (2026-05-24)


### Features

* **artwork:** resolve SGDB game via cascade with manual picker ([#762](https://github.com/danielcopper/decky-romm-sync/issues/762)) ([fc53363](https://github.com/danielcopper/decky-romm-sync/commit/fc53363fea4e6ac6ec3e01044f9127eb69e5d476))
* **library:** sync RomM smart collections ([#796](https://github.com/danielcopper/decky-romm-sync/issues/796)) ([c65b4ab](https://github.com/danielcopper/decky-romm-sync/commit/c65b4ab22ef309293077904822e866998c55087c))


### Bug Fixes

* **saves:** show focus outline on slot wizard buttons ([#768](https://github.com/danielcopper/decky-romm-sync/issues/768)) ([c3aedb6](https://github.com/danielcopper/decky-romm-sync/commit/c3aedb6930d04c98b8c2981929d151c5a8952fc1)), closes [#757](https://github.com/danielcopper/decky-romm-sync/issues/757)
* **ui:** skip non-scrollable ancestors in scroll-parent lookup ([#770](https://github.com/danielcopper/decky-romm-sync/issues/770)) ([06b694b](https://github.com/danielcopper/decky-romm-sync/commit/06b694b41baea7b06a8ed07a8c98800cb56a657c)), closes [#767](https://github.com/danielcopper/decky-romm-sync/issues/767)

## [0.18.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.17.1...decky-romm-sync-v0.18.0) (2026-05-21)


### Features

* **bootstrap:** add PluginMetadataReader Protocol + adapter ([#576](https://github.com/danielcopper/decky-romm-sync/issues/576)b) ([#606](https://github.com/danielcopper/decky-romm-sync/issues/606)) ([69d739d](https://github.com/danielcopper/decky-romm-sync/commit/69d739d72a924510438a5964a1f0d9be34250bd7))
* **bootstrap:** thread PluginMetadataReader through CallbackBundle ([#699](https://github.com/danielcopper/decky-romm-sync/issues/699)) ([085997f](https://github.com/danielcopper/decky-romm-sync/commit/085997fceb9c98f9ca0a248a2ef2e9e47423b0e5))
* **downloads:** emit download_failed event on download failure ([#632](https://github.com/danielcopper/decky-romm-sync/issues/632)) ([#651](https://github.com/danielcopper/decky-romm-sync/issues/651)) ([59526af](https://github.com/danielcopper/decky-romm-sync/commit/59526affc3e5c68f68999233881001acf1f636ae))
* **launch:** collapse 3 sequential callables into evaluate_launch ([#458](https://github.com/danielcopper/decky-romm-sync/issues/458)) ([#463](https://github.com/danielcopper/decky-romm-sync/issues/463)) ([6ddac44](https://github.com/danielcopper/decky-romm-sync/commit/6ddac443dfc970f58e46b775ce83a919e1954953))
* **lib:** introduce ListResult typed-subtype union ([#623](https://github.com/danielcopper/decky-romm-sync/issues/623)) ([#636](https://github.com/danielcopper/decky-romm-sync/issues/636)) ([189f956](https://github.com/danielcopper/decky-romm-sync/commit/189f95654b62007747bd36afcc8b6f9d3765e6cc))
* **library:** per-unit sync pipeline — incremental shortcut delivery ([#433](https://github.com/danielcopper/decky-romm-sync/issues/433)) ([5675d66](https://github.com/danielcopper/decky-romm-sync/commit/5675d66fe2420bbfc65a4cf0c491a329524e67d0))
* **lint:** add ESLint with React + a11y + hooks plugins ([#608](https://github.com/danielcopper/decky-romm-sync/issues/608)) ([#618](https://github.com/danielcopper/decky-romm-sync/issues/618)) ([628cc72](https://github.com/danielcopper/decky-romm-sync/commit/628cc7230d5c8ac7cb724113f6efdf2af4534839))
* **saves+ui:** ship pre-computed display fields on getSaveStatus/getBiosStatus ([#456](https://github.com/danielcopper/decky-romm-sync/issues/456)) ([#460](https://github.com/danielcopper/decky-romm-sync/issues/460)) ([cfa019e](https://github.com/danielcopper/decky-romm-sync/commit/cfa019ee95993f0c654293ca5868dbf588f37894))
* **saves:** add recommended_action to SaveSetupInfo; collapse SlotSetupWizard auto-confirm ([#457](https://github.com/danielcopper/decky-romm-sync/issues/457)) ([#462](https://github.com/danielcopper/decky-romm-sync/issues/462)) ([bb8c227](https://github.com/danielcopper/decky-romm-sync/commit/bb8c2278603c65176bc6132d778f628240310325))
* **scripts:** ban filesystem-touch patterns in services/ ([#673](https://github.com/danielcopper/decky-romm-sync/issues/673)) ([#676](https://github.com/danielcopper/decky-romm-sync/issues/676)) ([f65db18](https://github.com/danielcopper/decky-romm-sync/commit/f65db18e2df00948e1b053ed793bbbae1fb8ac0e))
* **session:** collapse handleGameStop into finalize_game_session ([#459](https://github.com/danielcopper/decky-romm-sync/issues/459)) ([#464](https://github.com/danielcopper/decky-romm-sync/issues/464)) ([27e8e19](https://github.com/danielcopper/decky-romm-sync/commit/27e8e195da59f0ccea2ee851ba7ddace8e3aff31))
* **test:** add @decky/api event-listener mock harness for component tests ([#701](https://github.com/danielcopper/decky-romm-sync/issues/701)) ([091ccc9](https://github.com/danielcopper/decky-romm-sync/commit/091ccc9990d909ce88c908414dc1be64355c9baa))
* **test:** bootstrap Vitest + RTL + Sonar lcov ingestion ([#616](https://github.com/danielcopper/decky-romm-sync/issues/616)) ([#633](https://github.com/danielcopper/decky-romm-sync/issues/633)) ([be18bcc](https://github.com/danielcopper/decky-romm-sync/commit/be18bcc60eab8d3839ec027d61bb7fd1fe19612a))
* **test:** introduce FakeRommApi fixture ([#662](https://github.com/danielcopper/decky-romm-sync/issues/662)) ([#665](https://github.com/danielcopper/decky-romm-sync/issues/665)) ([a1f629e](https://github.com/danielcopper/decky-romm-sync/commit/a1f629ee5b9336f0cb1a372014d3c14cd898f071))


### Bug Fixes

* **adapters:** set User-Agent on RomM + SteamGridDB requests ([#720](https://github.com/danielcopper/decky-romm-sync/issues/720)) ([2a71736](https://github.com/danielcopper/decky-romm-sync/commit/2a717367913054ab4a1f93c8429f035df2e386af)), closes [#249](https://github.com/danielcopper/decky-romm-sync/issues/249) [#719](https://github.com/danielcopper/decky-romm-sync/issues/719)
* **firmware:** use wall-clock for cache TTL ([#344](https://github.com/danielcopper/decky-romm-sync/issues/344)) ([#406](https://github.com/danielcopper/decky-romm-sync/issues/406)) ([f650e7c](https://github.com/danielcopper/decky-romm-sync/commit/f650e7c35a434113920c57b9140144c8a4369823))
* **launch_gate:** prevent silent abort→proceed inversion in ensureTrackingConfigured ([#656](https://github.com/danielcopper/decky-romm-sync/issues/656)) ([d64ee99](https://github.com/danielcopper/decky-romm-sync/commit/d64ee996d2853d1906a7e3a05e02b183a0f0538a))
* **launch_gate:** warn-not-allow when save-status check fails ([#629](https://github.com/danielcopper/decky-romm-sync/issues/629)) ([#638](https://github.com/danielcopper/decky-romm-sync/issues/638)) ([8f5e038](https://github.com/danielcopper/decky-romm-sync/commit/8f5e0382a234d60a61832e59356bda6fee925974))
* **library:** clear pending_prefetched_units on start_sync ([#555](https://github.com/danielcopper/decky-romm-sync/issues/555)) ([#580](https://github.com/danielcopper/decky-romm-sync/issues/580)) ([b603efe](https://github.com/danielcopper/decky-romm-sync/commit/b603efe0b96152934561a21fc3be4b393ecba95f))
* **library:** handle pagination failure without wiping shortcuts ([#630](https://github.com/danielcopper/decky-romm-sync/issues/630)) ([#641](https://github.com/danielcopper/decky-romm-sync/issues/641)) ([1610168](https://github.com/danielcopper/decky-romm-sync/commit/1610168c34f2bc0cb6ad8f695943b979ff75e81a))
* **library:** hoist refreshBios to function declaration ([#655](https://github.com/danielcopper/decky-romm-sync/issues/655)) ([b648670](https://github.com/danielcopper/decky-romm-sync/commit/b6486707b06d4501513de17792a588e52df7afb0))
* **library:** short-circuit apply for incremental-skip units ([#741](https://github.com/danielcopper/decky-romm-sync/issues/741)) ([38404bc](https://github.com/danielcopper/decky-romm-sync/commit/38404bca278d13c290b8981a610d4793efa51687))
* **library:** stop registry writes from clobbering peer-owned fields ([#746](https://github.com/danielcopper/decky-romm-sync/issues/746)) ([3ab1941](https://github.com/danielcopper/decky-romm-sync/commit/3ab194172fe11d6a08a37de565e702a1820e56b2))
* **library:** thread reporter + plugin_dir through configs ([#576](https://github.com/danielcopper/decky-romm-sync/issues/576)a) ([#605](https://github.com/danielcopper/decky-romm-sync/issues/605)) ([faa2416](https://github.com/danielcopper/decky-romm-sync/commit/faa2416cc14e5c5af77e5a6b324628051727c5f3))
* **main:** surface handleCancel status messages to the user ([#734](https://github.com/danielcopper/decky-romm-sync/issues/734)) ([8788797](https://github.com/danielcopper/decky-romm-sync/commit/87887970054db986f18de71895f9bd6f6451ffdc))
* **migration:** wire MigrationService task shutdown into plugin unload ([#731](https://github.com/danielcopper/decky-romm-sync/issues/731)) ([fe628fc](https://github.com/danielcopper/decky-romm-sync/commit/fe628fcf48b4277f1d7e6a7993c8e8fd24c8c8c5)), closes [#726](https://github.com/danielcopper/decky-romm-sync/issues/726)
* **python:** align dev/CI Python to Decky's embedded 3.11 ([#435](https://github.com/danielcopper/decky-romm-sync/issues/435)) ([76eb65b](https://github.com/danielcopper/decky-romm-sync/commit/76eb65b1ad0e791eb11c46bfd75b1de45745384d))
* **rom_removal:** include errors in uninstall_all_roms response ([#631](https://github.com/danielcopper/decky-romm-sync/issues/631)) ([#645](https://github.com/danielcopper/decky-romm-sync/issues/645)) ([06a41c8](https://github.com/danielcopper/decky-romm-sync/commit/06a41c83574c84e74bfd1ba5f3187bda1938da77))
* **saves:** block destructive delete-slot confirm when fetch failed ([#626](https://github.com/danielcopper/decky-romm-sync/issues/626)) ([#646](https://github.com/danielcopper/decky-romm-sync/issues/646)) ([b226784](https://github.com/danielcopper/decky-romm-sync/commit/b22678480c0bed9e264cbcd1c0091f820f061d63))
* **saves:** distinct server-unreachable status for rollback_to_version + list_file_versions ([#627](https://github.com/danielcopper/decky-romm-sync/issues/627)) ([#648](https://github.com/danielcopper/decky-romm-sync/issues/648)) ([e664ea5](https://github.com/danielcopper/decky-romm-sync/commit/e664ea5115dbffe00776b4e132407d4726f7c141))
* **saves:** inject HostnameProvider in SyncEngine ([CP] no-I/O) ([#515](https://github.com/danielcopper/decky-romm-sync/issues/515)) ([82a39eb](https://github.com/danielcopper/decky-romm-sync/commit/82a39eb2c19367be23550b6249edee43bde31507)), closes [#491](https://github.com/danielcopper/decky-romm-sync/issues/491)
* **saves:** persist file sync state on every upload path ([#409](https://github.com/danielcopper/decky-romm-sync/issues/409)) ([#445](https://github.com/danielcopper/decky-romm-sync/issues/445)) ([2977207](https://github.com/danielcopper/decky-romm-sync/commit/2977207b959c496bdfd2b1ebc2ce850bb799b70a))
* **saves:** persist slot promotion in PUT-path upload ([#346](https://github.com/danielcopper/decky-romm-sync/issues/346)) ([#408](https://github.com/danielcopper/decky-romm-sync/issues/408)) ([4aa7cd9](https://github.com/danielcopper/decky-romm-sync/commit/4aa7cd95d7fa858759b96761ec091e2146094a9a))
* **saves:** preserve slot config on delete + confirm before platform delete ([#281](https://github.com/danielcopper/decky-romm-sync/issues/281)) ([944b447](https://github.com/danielcopper/decky-romm-sync/commit/944b447d8fc15f92f95b51dbcf09ed65078088f9))
* **saves:** preserve slot map on list-saves API failure ([#625](https://github.com/danielcopper/decky-romm-sync/issues/625)) ([#644](https://github.com/danielcopper/decky-romm-sync/issues/644)) ([49b0625](https://github.com/danielcopper/decky-romm-sync/commit/49b0625196b8573588b60a65a08d2d05a320bafe))
* **saves:** record PUT uploads in own_upload_ids for correct attribution ([#749](https://github.com/danielcopper/decky-romm-sync/issues/749)) ([9dd96f9](https://github.com/danielcopper/decky-romm-sync/commit/9dd96f96efe7610d66532ad91b4e380d1c784a14)), closes [#276](https://github.com/danielcopper/decky-romm-sync/issues/276)
* **saves:** round-trip server_save_id through resolve_sync_conflict to close TOCTOU ([#384](https://github.com/danielcopper/decky-romm-sync/issues/384)) ([#446](https://github.com/danielcopper/decky-romm-sync/issues/446)) ([0e57d79](https://github.com/danielcopper/decky-romm-sync/commit/0e57d790631cee38bd6a1e376072289bc7da68b3))
* **saves:** sanitize server-supplied and frontend filenames ([#224](https://github.com/danielcopper/decky-romm-sync/issues/224)) ([#283](https://github.com/danielcopper/decky-romm-sync/issues/283)) ([a309b9d](https://github.com/danielcopper/decky-romm-sync/commit/a309b9d43228ce6460da3b801a708475994d6485))
* **saves:** split 'not_found' rollback status into ROM-not-installed vs version-deleted ([#653](https://github.com/danielcopper/decky-romm-sync/issues/653)) ([#674](https://github.com/danielcopper/decky-romm-sync/issues/674)) ([eba1532](https://github.com/danielcopper/decky-romm-sync/commit/eba1532fe1824c5fa4df7b8ca3843612cb7f61d1))
* **saves:** surface server-query-failed flag in get_save_status ([#628](https://github.com/danielcopper/decky-romm-sync/issues/628)) ([#649](https://github.com/danielcopper/decky-romm-sync/issues/649)) ([939ee53](https://github.com/danielcopper/decky-romm-sync/commit/939ee531af8d8af0297b2fc3e51f84ab0b8f25f5))
* **saves:** use server-canonical filename for both conflict-resolution paths ([#385](https://github.com/danielcopper/decky-romm-sync/issues/385)) ([#444](https://github.com/danielcopper/decky-romm-sync/issues/444)) ([4a4c893](https://github.com/danielcopper/decky-romm-sync/commit/4a4c893905fe2669e972d1e3a734210e82d77385))
* **saves:** wizard distinguishes server-unreachable from no-server-saves ([#624](https://github.com/danielcopper/decky-romm-sync/issues/624)) ([#650](https://github.com/danielcopper/decky-romm-sync/issues/650)) ([edee2a3](https://github.com/danielcopper/decky-romm-sync/commit/edee2a3a55389dc364e8638f1d7144daa446cef0))
* **session:** remove dead totalPausedMs writes ([#635](https://github.com/danielcopper/decky-romm-sync/issues/635)) ([9c3829c](https://github.com/danielcopper/decky-romm-sync/commit/9c3829c76e56fa1261f621a0dfca7d3c1b654b70)), closes [#634](https://github.com/danielcopper/decky-romm-sync/issues/634)
* **session:** wire SessionLifecycleService task shutdown into plugin unload ([#732](https://github.com/danielcopper/decky-romm-sync/issues/732)) ([aeaca4a](https://github.com/danielcopper/decky-romm-sync/commit/aeaca4a7b5d4be78083e823a6130378da10d74b2)), closes [#727](https://github.com/danielcopper/decky-romm-sync/issues/727)
* **sync:** reject sync_preview snapshots older than 30 minutes ([#345](https://github.com/danielcopper/decky-romm-sync/issues/345)) ([#407](https://github.com/danielcopper/decky-romm-sync/issues/407)) ([e924fa7](https://github.com/danielcopper/decky-romm-sync/commit/e924fa75cca190e9cb07bf138388baa767241dab))
* **tests:** unbreak migration save-sort error-injection tests ([#516](https://github.com/danielcopper/decky-romm-sync/issues/516)) ([12b6603](https://github.com/danielcopper/decky-romm-sync/commit/12b6603181d4b5bbc35cf26aabd135730e5b9234)), closes [#493](https://github.com/danielcopper/decky-romm-sync/issues/493)
* **ui:** make backend authoritative for sync progress, fix indicator + button state ([#754](https://github.com/danielcopper/decky-romm-sync/issues/754)) ([3fcbac4](https://github.com/danielcopper/decky-romm-sync/commit/3fcbac4938c42572078ae266f85b3416ef00bb44))
* **ui:** re-read cancelled closure in refreshBiosInBackground ([#730](https://github.com/danielcopper/decky-romm-sync/issues/730)) ([202cc12](https://github.com/danielcopper/decky-romm-sync/commit/202cc12954db749c4739d38bc07086c81ecb43e0))

## [0.17.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.17.0...decky-romm-sync-v0.17.1) (2026-05-07)


### Bug Fixes

* **game-detail:** scroll to top on focus of action buttons ([#162](https://github.com/danielcopper/decky-romm-sync/issues/162)) ([#270](https://github.com/danielcopper/decky-romm-sync/issues/270)) ([6bd28c1](https://github.com/danielcopper/decky-romm-sync/commit/6bd28c1e4d00d980804ed2ef2b46ad8a7566402b))
* **qam:** scroll and focus to top on QAM page navigation ([#161](https://github.com/danielcopper/decky-romm-sync/issues/161)) ([#266](https://github.com/danielcopper/decky-romm-sync/issues/266)) ([4ddd473](https://github.com/danielcopper/decky-romm-sync/commit/4ddd473f7ca4ad282e72788ffb0d5425a841ff31))

## [0.17.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.16.0...decky-romm-sync-v0.17.0) (2026-05-04)


### ⚠ BREAKING CHANGES

* **downloads:** Existing installs of nested single-file ROMs were stored locally without their file extension. Affected ROMs must be re-downloaded (or re-synced via the plugin) after updating so the on-disk filename is corrected — buggy entries cannot be patched in place.

### Bug Fixes

* **downloads:** preserve file extension for nested single-file ROMs ([#226](https://github.com/danielcopper/decky-romm-sync/issues/226)) ([#263](https://github.com/danielcopper/decky-romm-sync/issues/263)) ([dbe14f4](https://github.com/danielcopper/decky-romm-sync/commit/dbe14f47f1c586db3d2f6ba781f0ae6bc54e7388))
* **migration:** block all ops during pending RetroDECK path migration ([#261](https://github.com/danielcopper/decky-romm-sync/issues/261)) ([afd5939](https://github.com/danielcopper/decky-romm-sync/commit/afd59393be88e4f1c032448a08475668e8ffc18f))

## [0.16.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.15.0...decky-romm-sync-v0.16.0) (2026-05-03)


### ⚠ BREAKING CHANGES

* **api:** This plugin now requires RomM server 4.8.1 or newer. Servers running 4.7.x or 4.8.0 are hard-rejected with a full error page and the plugin is inert. You MUST update your RomM server to exactly 4.8.1 or newer before installing this release — there is no graceful fallback.

### Features

* **saves:** auto-detect migration state across plugin lifecycle ([#240](https://github.com/danielcopper/decky-romm-sync/issues/240)) ([94c7e78](https://github.com/danielcopper/decky-romm-sync/commit/94c7e78c6f8c58babfb2967ed963cc42b1f4b720))
* **saves:** device list + client_version reconciliation ([#247](https://github.com/danielcopper/decky-romm-sync/issues/247)) ([2428f66](https://github.com/danielcopper/decky-romm-sync/commit/2428f661d18f3c1ff9a9cddd3e9a6e4b995229d1))
* **saves:** redesign saves tab with slot-based collapsible layout ([#220](https://github.com/danielcopper/decky-romm-sync/issues/220)) ([ebb4d56](https://github.com/danielcopper/decky-romm-sync/commit/ebb4d567df7e8c73a6359651996c624b321d4487))
* **saves:** save version history and rollback UI ([#225](https://github.com/danielcopper/decky-romm-sync/issues/225)) ([4bc8ff1](https://github.com/danielcopper/decky-romm-sync/commit/4bc8ff1a03347565e08116fecc9931b8a543d19b))
* **saves:** show offline indicators in play section and saves tab ([#221](https://github.com/danielcopper/decky-romm-sync/issues/221)) ([#223](https://github.com/danielcopper/decky-romm-sync/issues/223)) ([78331c0](https://github.com/danielcopper/decky-romm-sync/commit/78331c0badf19156c33d31ab6910caa1c369efa3))
* **saves:** show success toast on migration completion ([#234](https://github.com/danielcopper/decky-romm-sync/issues/234)) ([82f7af8](https://github.com/danielcopper/decky-romm-sync/commit/82f7af8bfd08a1e577e4b934826adb2a9782d3b1))
* **saves:** slot deletion with server capabilities system ([#245](https://github.com/danielcopper/decky-romm-sync/issues/245)) ([b59e231](https://github.com/danielcopper/decky-romm-sync/commit/b59e231d432c9300c9f20dba9a6f048199180ecd))


### Bug Fixes

* **saves:** close post-exit sync race during pending save-sort migration ([#241](https://github.com/danielcopper/decky-romm-sync/issues/241)) ([4a204bb](https://github.com/danielcopper/decky-romm-sync/commit/4a204bbf88de900cc0c7fd1d4dacdd1ad412de33))
* **saves:** resolve corename from retroarch .info, split retrodeck config adapter ([#227](https://github.com/danielcopper/decky-romm-sync/issues/227)) ([2f27f8e](https://github.com/danielcopper/decky-romm-sync/commit/2f27f8e563fc0fede0a39f0ac981df2821157c50))
* **saves:** resolve corename via retroarch .info for sort-by-core save paths ([#233](https://github.com/danielcopper/decky-romm-sync/issues/233)) ([089e455](https://github.com/danielcopper/decky-romm-sync/commit/089e4559e65de4fa158e15340270c8feb96c76ef))


### Miscellaneous Chores

* **api:** require RomM 4.8.1, drop v4.6 support, polish version error UI ([#246](https://github.com/danielcopper/decky-romm-sync/issues/246)) ([7f616f7](https://github.com/danielcopper/decky-romm-sync/commit/7f616f7034a0b3149783d0b1182bc6dec6504d52))

## [0.15.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.14.0...decky-romm-sync-v0.15.0) (2026-04-01)


### Features

* **adapters:** SaveApiV47 device sync methods ([#182](https://github.com/danielcopper/decky-romm-sync/issues/182)) ([#187](https://github.com/danielcopper/decky-romm-sync/issues/187)) ([ef1340e](https://github.com/danielcopper/decky-romm-sync/commit/ef1340eb987d34301152cee207f382632e0b0634))
* **domain:** save sync v2 domain logic ([#183](https://github.com/danielcopper/decky-romm-sync/issues/183)) ([#189](https://github.com/danielcopper/decky-romm-sync/issues/189)) ([b3bab71](https://github.com/danielcopper/decky-romm-sync/commit/b3bab71a60df28bb6492d9b3858b8e01cb411bab))
* Save Sync v2 Frontend — device info, slots, device sync status ([#185](https://github.com/danielcopper/decky-romm-sync/issues/185)) ([#191](https://github.com/danielcopper/decky-romm-sync/issues/191)) ([2ce96be](https://github.com/danielcopper/decky-romm-sync/commit/2ce96bed44ca379a1a11da4580cfed487970a8d1))
* **saves:** expand save file extensions for DS and Sega CD ([#196](https://github.com/danielcopper/decky-romm-sync/issues/196)) ([#204](https://github.com/danielcopper/decky-romm-sync/issues/204)) ([e57b51b](https://github.com/danielcopper/decky-romm-sync/commit/e57b51bb1783ffecff42a680f744d9e1694ff27a))
* **saves:** save sync v2 service refactoring ([#184](https://github.com/danielcopper/decky-romm-sync/issues/184)) ([#190](https://github.com/danielcopper/decky-romm-sync/issues/190)) ([7eebe41](https://github.com/danielcopper/decky-romm-sync/commit/7eebe41b8bdcf502b39ae3fb83cf39e60368e8bf))
* **saves:** unify save status check — single non-blocking background check ([#201](https://github.com/danielcopper/decky-romm-sync/issues/201)) ([#202](https://github.com/danielcopper/decky-romm-sync/issues/202)) ([3b63893](https://github.com/danielcopper/decky-romm-sync/commit/3b63893eb907fd3552eb9ea01a77b765927a0573))
* **ui:** core-switch warning, controller navigation, BiosFileEntry fix ([#198](https://github.com/danielcopper/decky-romm-sync/issues/198)) ([#212](https://github.com/danielcopper/decky-romm-sync/issues/212)) ([0c7013c](https://github.com/danielcopper/decky-romm-sync/commit/0c7013ca0d3b719e5159dbe7c071cb306ba25dc8))


### Bug Fixes

* **launcher:** replace shell interpolation with env vars and remove download queue ([#118](https://github.com/danielcopper/decky-romm-sync/issues/118)) ([#209](https://github.com/danielcopper/decky-romm-sync/issues/209)) ([732bdbf](https://github.com/danielcopper/decky-romm-sync/commit/732bdbfc94878430cd3d5093652a522802f1f87c))
* **saves:** filter server saves by active_slot in matching logic ([#200](https://github.com/danielcopper/decky-romm-sync/issues/200)) ([#203](https://github.com/danielcopper/decky-romm-sync/issues/203)) ([30b74fb](https://github.com/danielcopper/decky-romm-sync/commit/30b74fbb55f73da1ff07b5d4592c5fd70ab89df8))

## [0.14.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.13.1...decky-romm-sync-v0.14.0) (2026-03-20)


### Features

* **collections:** sync RomM collections to Steam collections ([#106](https://github.com/danielcopper/decky-romm-sync/issues/106)) ([#173](https://github.com/danielcopper/decky-romm-sync/issues/173)) ([16e68d2](https://github.com/danielcopper/decky-romm-sync/commit/16e68d222a5c6896c8cc28b5138c805b26cde345))
* improve default whitelisting for non-Steam game removal ([#137](https://github.com/danielcopper/decky-romm-sync/issues/137)) ([11c02f1](https://github.com/danielcopper/decky-romm-sync/commit/11c02f1fbeddbe193b0f4aeed3e509afc2f07a1f))


### Bug Fixes

* firmware cache + async BIOS on game detail page ([#148](https://github.com/danielcopper/decky-romm-sync/issues/148)) ([7a7f408](https://github.com/danielcopper/decky-romm-sync/commit/7a7f40868931022c2a1dbae8141c7ac5e271ee13))
* **persistence:** add file locking + schema versioning ([#120](https://github.com/danielcopper/decky-romm-sync/issues/120), [#121](https://github.com/danielcopper/decky-romm-sync/issues/121)) ([#153](https://github.com/danielcopper/decky-romm-sync/issues/153)) ([5f13e99](https://github.com/danielcopper/decky-romm-sync/commit/5f13e999c11c3da27c5d4563a6591fec91fd7aa1))
* progressive read timeout for large file downloads ([#139](https://github.com/danielcopper/decky-romm-sync/issues/139)) ([0988e49](https://github.com/danielcopper/decky-romm-sync/commit/0988e4909cef685798cad978956d431a85e3e2fa))

## [0.13.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.13.0...decky-romm-sync-v0.13.1) (2026-03-16)


### Bug Fixes

* code quality fixes — external review, SonarCloud, encapsulation ([#108](https://github.com/danielcopper/decky-romm-sync/issues/108)) ([8dfb215](https://github.com/danielcopper/decky-romm-sync/commit/8dfb21511c7ba54e9ab708a2d374d7f3d3573905))

## [0.13.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.12.0...decky-romm-sync-v0.13.0) (2026-03-15)


### Features

* detect and display RomM server version ([#98](https://github.com/danielcopper/decky-romm-sync/issues/98)) ([561cf0d](https://github.com/danielcopper/decky-romm-sync/commit/561cf0d923791d1535a10689579be8305a3b75ef))
* v47 SaveApi adapter + VersionRouter + bug fixes ([#103](https://github.com/danielcopper/decky-romm-sync/issues/103)) ([cff8709](https://github.com/danielcopper/decky-romm-sync/commit/cff8709d66416b888f7bef2cf37ec6901a67a0ea))


### Bug Fixes

* retry app ID init on boot when backend isn't ready ([#95](https://github.com/danielcopper/decky-romm-sync/issues/95)) ([131279c](https://github.com/danielcopper/decky-romm-sync/commit/131279c071cc9e9f5624833974ca0e6ef584e075))

## [0.12.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.11.0...decky-romm-sync-v0.12.0) (2026-03-12)


### Features

* download button animation with progress fill and state transitions ([#84](https://github.com/danielcopper/decky-romm-sync/issues/84)) ([e70861a](https://github.com/danielcopper/decky-romm-sync/commit/e70861a1e15537bb770a612c737f28beb477ff76))
* Phase 7 RetroAchievements - backend, frontend, and game detail tabs (WIP) ([#86](https://github.com/danielcopper/decky-romm-sync/issues/86)) ([3f6a6f7](https://github.com/danielcopper/decky-romm-sync/commit/3f6a6f71a0016b26044a7ee527afe1be599f49a7))


### Bug Fixes

* controller scrolling through injected game detail content ([#87](https://github.com/danielcopper/decky-romm-sync/issues/87)) ([cd8e4ce](https://github.com/danielcopper/decky-romm-sync/commit/cd8e4ce33e1ea872b8bbc23a73ee2a660f6aa056))
* move HC badge before date in achievement list ([#88](https://github.com/danielcopper/decky-romm-sync/issues/88)) ([ded3ddc](https://github.com/danielcopper/decky-romm-sync/commit/ded3ddc0ae78c5521333064bf511e8108e55443f))
* retry app ID init on boot when backend isn't ready ([#94](https://github.com/danielcopper/decky-romm-sync/issues/94)) ([3e24dc2](https://github.com/danielcopper/decky-romm-sync/commit/3e24dc2e5f9b9571c0ff19924f9796ba1b38dc37))
* review cycle fixes — security, React cleanup, linting, type safety ([#93](https://github.com/danielcopper/decky-romm-sync/issues/93)) ([1ab7dea](https://github.com/danielcopper/decky-romm-sync/commit/1ab7dea319154c8a034ef10a5f5ebc9f0cbb7301))
* Tier 1 bug fixes — correctness, security, state management ([#89](https://github.com/danielcopper/decky-romm-sync/issues/89)) ([6125343](https://github.com/danielcopper/decky-romm-sync/commit/6125343c0a92350605129dcc1b7ee992644a23f4))
* Tier 2 robustness and performance improvements ([#90](https://github.com/danielcopper/decky-romm-sync/issues/90)) ([17bea27](https://github.com/danielcopper/decky-romm-sync/commit/17bea276c72034def77b2415e14c897af19ecce0))
* Tier 3 improvements — caching, serialization, cleanup ([#91](https://github.com/danielcopper/decky-romm-sync/issues/91)) ([a8f93e3](https://github.com/danielcopper/decky-romm-sync/commit/a8f93e3dfd7fea4213da119beacb33cadacb2148))

## [0.11.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.10.1...decky-romm-sync-v0.11.0) (2026-03-09)


### Features

* compact inline status display in QAM main page ([#82](https://github.com/danielcopper/decky-romm-sync/issues/82)) ([d505eb1](https://github.com/danielcopper/decky-romm-sync/commit/d505eb10438ef7b13c8d6d91b02cdfa44d03b548))

## [0.10.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.10.0...decky-romm-sync-v0.10.1) (2026-03-09)


### Bug Fixes

* don't show migration warning on fresh install ([#80](https://github.com/danielcopper/decky-romm-sync/issues/80)) ([c78d703](https://github.com/danielcopper/decky-romm-sync/commit/c78d7033972e21d534dd573138b595c73a9134d3))

## [0.10.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.9.5...decky-romm-sync-v0.10.0) (2026-03-09)


### Features

* delta sync with preview before apply ([#76](https://github.com/danielcopper/decky-romm-sync/issues/76)) ([8060710](https://github.com/danielcopper/decky-romm-sync/commit/80607101e841c7883522d1a42bf7503458be3051))
* frontend error differentiation with user-friendly messages ([#73](https://github.com/danielcopper/decky-romm-sync/issues/73)) ([18ec727](https://github.com/danielcopper/decky-romm-sync/commit/18ec72770be29a14c05e2145bdddef221b90349b))


### Bug Fixes

* download queue pruning and async blocking I/O audit (EXT-3, EXT-5) ([#75](https://github.com/danielcopper/decky-romm-sync/issues/75)) ([75d5cb0](https://github.com/danielcopper/decky-romm-sync/commit/75d5cb03e7ebbf6ed638b8645c9d0828a29dc1e0))
* resolve 8 Dependabot security alerts (minimatch ReDoS, rollup path traversal) ([#78](https://github.com/danielcopper/decky-romm-sync/issues/78)) ([57114c2](https://github.com/danielcopper/decky-romm-sync/commit/57114c28058c38e20ca5e3445c815d89f7a84d8c))

## [0.9.5](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.9.4...decky-romm-sync-v0.9.5) (2026-03-07)


### Bug Fixes

* hide native Steam tabs on RomM game detail pages ([#69](https://github.com/danielcopper/decky-romm-sync/issues/69)) ([4046f1e](https://github.com/danielcopper/decky-romm-sync/commit/4046f1eac1298c9dd7656386c3b85def7ba4dac4))

## [0.9.4](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.9.3...decky-romm-sync-v0.9.4) (2026-03-06)


### Bug Fixes

* resolve defaults/ file paths after lib move to py_modules/ ([#67](https://github.com/danielcopper/decky-romm-sync/issues/67)) ([8ff95b0](https://github.com/danielcopper/decky-romm-sync/commit/8ff95b0006bd15bb785f7b30982a4c1f7c80aec9))

## [0.9.3](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.9.2...decky-romm-sync-v0.9.3) (2026-02-27)


### Bug Fixes

* move lib/ into py_modules/ for Decky CLI packaging ([#65](https://github.com/danielcopper/decky-romm-sync/issues/65)) ([9e89e5e](https://github.com/danielcopper/decky-romm-sync/commit/9e89e5e8874c2b1b19d4ecf3b577ad9772123af4))

## [0.9.2](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.9.1...decky-romm-sync-v0.9.2) (2026-02-27)


### Bug Fixes

* pre-beta review — bug fixes + docs ([#63](https://github.com/danielcopper/decky-romm-sync/issues/63)) ([0e1e271](https://github.com/danielcopper/decky-romm-sync/commit/0e1e2715ecd8ec39afc10b07ff036ae5225df0bf))

## [0.9.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.9.0...decky-romm-sync-v0.9.1) (2026-02-27)


### Bug Fixes

* BIOS detail — all files with per-core annotations ([#60](https://github.com/danielcopper/decky-romm-sync/issues/60)) ([c919348](https://github.com/danielcopper/decky-romm-sync/commit/c9193486cadfa4dc804775b77c194de6b7e13e9d))

## [0.9.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.8.3...decky-romm-sync-v0.9.0) (2026-02-27)


### Features

* core switching UI — per-platform and per-game ([#59](https://github.com/danielcopper/decky-romm-sync/issues/59)) ([50c8987](https://github.com/danielcopper/decky-romm-sync/commit/50c8987cda9bab9ac8b5e197dd42f7c7827a86e4))
* per-core BIOS filtering ([#57](https://github.com/danielcopper/decky-romm-sync/issues/57)) ([171b9d6](https://github.com/danielcopper/decky-romm-sync/commit/171b9d6eb586f8d41d333b14bee0726cec607676))

## [0.8.3](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.8.2...decky-romm-sync-v0.8.3) (2026-02-27)


### Bug Fixes

* BIOS status reporting + RetroDECK path resolution ([#56](https://github.com/danielcopper/decky-romm-sync/issues/56)) ([220df10](https://github.com/danielcopper/decky-romm-sync/commit/220df10ec07538d36ff24813dc890b25e7e16009))
* enforce 0600 permissions on settings.json ([#55](https://github.com/danielcopper/decky-romm-sync/issues/55)) ([921ab48](https://github.com/danielcopper/decky-romm-sync/commit/921ab48fec7fed4474d7a8737af15db0b9bd0f3f))
* restore BIOS badge in game detail PlaySection ([#53](https://github.com/danielcopper/decky-romm-sync/issues/53)) ([f86c867](https://github.com/danielcopper/decky-romm-sync/commit/f86c8675ac8671269d60191b8b5fbf415a39d81e))

## [0.8.2](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.8.1...decky-romm-sync-v0.8.2) (2026-02-25)


### Bug Fixes

* SSL certificate verification + HTTP client consolidation ([#51](https://github.com/danielcopper/decky-romm-sync/issues/51)) ([4a5e4a8](https://github.com/danielcopper/decky-romm-sync/commit/4a5e4a8c96f89bce8f37fdcf1b3818f7025bc70b))

## [0.8.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.8.0...decky-romm-sync-v0.8.1) (2026-02-25)


### Bug Fixes

* remove install status badge, move platform to game info section ([#50](https://github.com/danielcopper/decky-romm-sync/issues/50)) ([36f09b7](https://github.com/danielcopper/decky-romm-sync/commit/36f09b7da8036b2fbdaf4a4a36e9695dbc1d93c0))
* startup state healing — atomic settings, orphan cleanup, tmp pruning ([#48](https://github.com/danielcopper/decky-romm-sync/issues/48)) ([5b635be](https://github.com/danielcopper/decky-romm-sync/commit/5b635be50c3bd0ffde58f82d6d1fbb91670f3b9c))

## [0.8.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.7.0...decky-romm-sync-v0.8.0) (2026-02-25)


### Features

* Phase 5.6 remaining — cache-first game detail, save sync improvements ([#45](https://github.com/danielcopper/decky-romm-sync/issues/45)) ([7d5ca4d](https://github.com/danielcopper/decky-romm-sync/commit/7d5ca4dbf80eeab9141fed314c08614845c5401d))


### Bug Fixes

* sync & download progress bars, cancel sync ([#47](https://github.com/danielcopper/decky-romm-sync/issues/47)) ([27a4aff](https://github.com/danielcopper/decky-romm-sync/commit/27a4affef5e5110ccee073ba00f2d2099a803509))

## [0.7.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.6.0...decky-romm-sync-v0.7.0) (2026-02-25)


### Features

* frontend logging overhaul — log level system with console.* migration ([#42](https://github.com/danielcopper/decky-romm-sync/issues/42)) ([a90ac50](https://github.com/danielcopper/decky-romm-sync/commit/a90ac507d528a54a8b6d9332e0462dba4b402cae))

## [0.6.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.5.0...decky-romm-sync-v0.6.0) (2026-02-24)


### Features

* delete local save files and BIOS files ([#41](https://github.com/danielcopper/decky-romm-sync/issues/41)) ([d460600](https://github.com/danielcopper/decky-romm-sync/commit/d460600eb37166f9b9b23743a59073318706358e))


### Bug Fixes

* gear icon buttons mouse/touch clicks and Properties dialog ([#39](https://github.com/danielcopper/decky-romm-sync/issues/39)) ([55f45ed](https://github.com/danielcopper/decky-romm-sync/commit/55f45ed549c55daf4dc1456eac078419b693f67d))

## [0.5.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.4.0...decky-romm-sync-v0.5.0) (2026-02-23)


### Features

* pre-launch save sync with conflict detection and resolution UI ([#37](https://github.com/danielcopper/decky-romm-sync/issues/37)) ([516b8b1](https://github.com/danielcopper/decky-romm-sync/commit/516b8b15c3340a3b62c6524f4132714721be6a5c))

## [0.4.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.3.0...decky-romm-sync-v0.4.0) (2026-02-23)


### Features

* Phase 5.6 — Restyle game detail page ([#35](https://github.com/danielcopper/decky-romm-sync/issues/35)) ([66e08e8](https://github.com/danielcopper/decky-romm-sync/commit/66e08e8b33d4d88f1b195e94307d13f1b57dcab5))

## [0.3.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.2.1...decky-romm-sync-v0.3.0) (2026-02-21)


### Features

* Phase 5 — save file sync and custom PlaySection ([#34](https://github.com/danielcopper/decky-romm-sync/issues/34)) ([5c24b79](https://github.com/danielcopper/decky-romm-sync/commit/5c24b7964afab9a9f5eb322bd2bb574effe7b7b2))


### Bug Fixes

* Phase 4.5 bug fixes — DangerZone, Remote Play, scoped collections ([#32](https://github.com/danielcopper/decky-romm-sync/issues/32)) ([8f06776](https://github.com/danielcopper/decky-romm-sync/commit/8f067769219a5cf159c956f52315553b3a87115c))

## [0.2.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.2.0...decky-romm-sync-v0.2.1) (2026-02-17)


### Bug Fixes

* rename backend/ to lib/ to avoid Decky CLI build conflict ([#30](https://github.com/danielcopper/decky-romm-sync/issues/30)) ([fee6176](https://github.com/danielcopper/decky-romm-sync/commit/fee61768f19ddb97464bf8fdf90a2912f1dfda10))

## [0.2.0](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.6...decky-romm-sync-v0.2.0) (2026-02-17)


### Features

* Phase 4A — SteamGridDB artwork + metadata UX ([#25](https://github.com/danielcopper/decky-romm-sync/issues/25)) ([37c54c8](https://github.com/danielcopper/decky-romm-sync/commit/37c54c8d627ff22edd61fd972d2a0de639dbf0ac))
* Phase 4B — native metadata via store patching ([#27](https://github.com/danielcopper/decky-romm-sync/issues/27)) ([a03e0d2](https://github.com/danielcopper/decky-romm-sync/commit/a03e0d2b0972d65ba3977d33cf1f3f8776b28189))

## [0.1.6](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.5...decky-romm-sync-v0.1.6) (2026-02-16)


### Bug Fixes

* bundle py_modules/vdf in repo for Decky CLI builds ([#23](https://github.com/danielcopper/decky-romm-sync/issues/23)) ([6094eae](https://github.com/danielcopper/decky-romm-sync/commit/6094eae94cf5b22d4b01c2b8ae10dcad573fd7b6))

## [0.1.5](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.4...decky-romm-sync-v0.1.5) (2026-02-16)


### Bug Fixes

* add requirements.txt for Decky CLI Python dependency bundling ([#19](https://github.com/danielcopper/decky-romm-sync/issues/19)) ([ca0c841](https://github.com/danielcopper/decky-romm-sync/commit/ca0c84152c9de293bedd214f577cf703db1f107d))

## [0.1.4](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.3...decky-romm-sync-v0.1.4) (2026-02-16)


### Bug Fixes

* OSK focus loss and test connection blocking ([#17](https://github.com/danielcopper/decky-romm-sync/issues/17)) ([0d10d6c](https://github.com/danielcopper/decky-romm-sync/commit/0d10d6ced410728946e9d63600d87b06a84a543b))

## [0.1.3](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.2...decky-romm-sync-v0.1.3) (2026-02-16)


### Bug Fixes

* CI upload when zip already named correctly ([#15](https://github.com/danielcopper/decky-romm-sync/issues/15)) ([523a447](https://github.com/danielcopper/decky-romm-sync/commit/523a44759763c0865a55d62ea7120b4b61621b3b))

## [0.1.2](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.1...decky-romm-sync-v0.1.2) (2026-02-16)


### Bug Fixes

* add @rollup/rollup-linux-x64-musl for Decky builder CI ([#13](https://github.com/danielcopper/decky-romm-sync/issues/13)) ([ad5043b](https://github.com/danielcopper/decky-romm-sync/commit/ad5043bb3b9921ca4b3e22330c6fdf3467af791e))

## [0.1.1](https://github.com/danielcopper/decky-romm-sync/compare/decky-romm-sync-v0.1.0...decky-romm-sync-v0.1.1) (2026-02-16)


### Bug Fixes

* add version field to plugin.json for release-please ([#11](https://github.com/danielcopper/decky-romm-sync/issues/11)) ([20272c9](https://github.com/danielcopper/decky-romm-sync/commit/20272c9c7c400fe8580907143f4368d5a9983135))

## 0.1.0 (2026-02-16)


### Features

* Phase 1 — plugin skeleton, settings UI, RomM connection ([#1](https://github.com/danielcopper/romm-library/issues/1)) ([f3ce7c3](https://github.com/danielcopper/romm-library/commit/f3ce7c3bf6fe80484b24649530ec307d4aeede93))
* Phase 2 — sync engine, Steam shortcuts, artwork & collections ([#3](https://github.com/danielcopper/romm-library/issues/3)) ([b6e58ac](https://github.com/danielcopper/romm-library/commit/b6e58ac3b3ab31f9d70f9324e6901fd6a7304c3e))
* Phase 3 — download manager, security hardening, 100 tests ([#6](https://github.com/danielcopper/romm-library/issues/6)) ([fa78b1c](https://github.com/danielcopper/romm-library/commit/fa78b1cff20358702809724862f6e16ee21a6d8a))


### Bug Fixes

* Phase 3.5 bug fixes — BIOS, RetroArch input, Steam Input ([#7](https://github.com/danielcopper/romm-library/issues/7)) ([5f34f2d](https://github.com/danielcopper/romm-library/commit/5f34f2dcd9d62299c3c99914354223e30a45dc2c))
