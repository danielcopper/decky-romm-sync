# Changelog

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
