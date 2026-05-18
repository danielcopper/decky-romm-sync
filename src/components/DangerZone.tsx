import { useState, useEffect, useMemo, FC, ReactNode, createElement } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ConfirmModal,
  Field,
  TextField,
  ToggleField,
  Spinner,
  ModalRoot,
  DialogButton,
  showModal,
} from "@decky/ui";
import {
  getRegistryPlatforms,
  removePlatformShortcuts,
  removeAllShortcuts,
  reportRemovalResults,
  uninstallAllRoms,
  deletePlatformSaves,
  deletePlatformBios,
  logInfo,
  logWarn,
  logError,
  getWhitelistSettings,
  updateWhitelistSettings,
} from "../api/backend";
import { removeShortcut } from "../utils/steamShortcuts";
import { scrollToTop } from "../utils/scrollHelpers";
import { clearPlatformCollection, clearAllRomMCollections } from "../utils/collections";
import { formatUninstallStatus } from "../utils/formatters";
import type { RegistryPlatform } from "../types";

const DEFAULT_WHITELIST_PATTERNS: string[] = [
  "retrodeck", "moonlight", "chiaki", "chrome", "chromium",
  "firefox", "vivaldi", "heroic", "lutris", "bottles",
  "protonup", "emudeck", "desktop mode", "return to gaming mode",
  "nonsteamlaunchers",
];

// Fuzzy match: each character of the query must appear in order in the target (like fzf)
const fuzzyMatch = (query: string, target: string): boolean => {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
};

interface NonSteamApp {
  appId: number;
  name: string;
}

const PlatformActionModal: FC<{
  platform: RegistryPlatform;
  closeModal?: () => void;
  onRemoveShortcuts: () => void;
  onDeleteSaves: () => void;
  onDeleteBios: () => void;
}> = ({ platform, closeModal, onRemoveShortcuts, onDeleteSaves, onDeleteBios }) => (
  <ModalRoot closeModal={closeModal}>
    <div style={{ padding: "16px", minWidth: "320px" }}>
      <div style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", marginBottom: "16px" }}>
        Actions for {platform.name}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <DialogButton onClick={() => { closeModal?.(); onRemoveShortcuts(); }}>
          Remove Shortcuts ({platform.count} game{platform.count === 1 ? "" : "s"})
        </DialogButton>
        <DialogButton onClick={() => { closeModal?.(); onDeleteSaves(); }}>
          Delete Save Files
        </DialogButton>
        <DialogButton onClick={() => { closeModal?.(); onDeleteBios(); }}>
          Delete BIOS Files
        </DialogButton>
        <DialogButton onClick={() => closeModal?.()} style={{ opacity: 0.5 }}>
          Cancel
        </DialogButton>
      </div>
    </div>
  </ModalRoot>
);

interface ShortcutRemovalSectionProps {
  platforms: RegistryPlatform[];
  loading: boolean;
  refreshPlatforms: () => Promise<void>;
  loadNonSteamApps: () => void;
  status: string;
  setStatus: (s: string) => void;
}

const ShortcutRemovalSection: FC<ShortcutRemovalSectionProps> = ({
  platforms,
  loading,
  refreshPlatforms,
  loadNonSteamApps,
  status,
  setStatus,
}) => {
  const [actionStatus, setActionStatus] = useState("");
  const [uninstallStatus, setUninstallStatus] = useState("");
  const [confirmRemoveAllRomm, setConfirmRemoveAllRomm] = useState(false);
  const [confirmUninstall, setConfirmUninstall] = useState(false);

  const handleRemoveShortcuts = async (p: RegistryPlatform) => {
    setActionStatus(`Removing ${p.name} shortcuts...`);
    try {
      const result = await removePlatformShortcuts(p.slug);
      if (result.app_ids) {
        for (const appId of result.app_ids) {
          removeShortcut(appId);
        }
      }
      if (result.rom_ids?.length) {
        await reportRemovalResults(result.rom_ids);
      }
      await clearPlatformCollection(result.platform_name || p.name);
      setActionStatus(`Removed ${p.count} ${p.name} game${p.count === 1 ? "" : "s"}`);
      await refreshPlatforms();
      loadNonSteamApps();
    } catch {
      setActionStatus("Failed to remove shortcuts");
    }
  };

  const handleDeleteSaves = (p: RegistryPlatform) => {
    const platformName = p.name || p.slug;
    showModal(
      createElement(ConfirmModal, {
        strTitle: `Delete all save files for ${platformName}?`,
        strDescription:
          "This will delete every local save file for ROMs on this platform. Any local changes that haven't been uploaded to RomM yet will be lost permanently. Make sure saves are synced first.",
        strOKButtonText: "Delete Save Files",
        strCancelButtonText: "Cancel",
        onOK: async () => {
          setActionStatus(`Deleting ${p.name} saves...`);
          try {
            const result = await deletePlatformSaves(p.slug);
            setActionStatus(result.message);
            globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync" } }));
          } catch {
            setActionStatus("Failed to delete saves");
          }
        },
      }),
    );
  };

  const handleDeleteBios = async (p: RegistryPlatform) => {
    setActionStatus(`Deleting ${p.name} BIOS...`);
    try {
      const result = await deletePlatformBios(p.slug);
      setActionStatus(result.message);
    } catch {
      setActionStatus("Failed to delete BIOS files");
    }
  };

  const handleRemoveAllRomm = async () => {
    if (!confirmRemoveAllRomm) {
      setConfirmRemoveAllRomm(true);
      return;
    }
    setConfirmRemoveAllRomm(false);
    setStatus("Removing all shortcuts...");
    const result = await removeAllShortcuts();
    if (result.app_ids) {
      for (const appId of result.app_ids) {
        removeShortcut(appId);
      }
    }
    if (result.rom_ids?.length) {
      await reportRemovalResults(result.rom_ids);
    }
    await clearAllRomMCollections();
    setStatus(result.message);
    await refreshPlatforms();
    loadNonSteamApps();
  };

  const handleUninstallAll = async () => {
    if (!confirmUninstall) {
      setConfirmUninstall(true);
      return;
    }
    try {
      setUninstallStatus("Uninstalling...");
      const result = await uninstallAllRoms();
      setUninstallStatus(formatUninstallStatus(result.removed_count, result.errors.length));
    } catch {
      setUninstallStatus("Failed to uninstall ROMs");
    }
    setConfirmUninstall(false);
    await refreshPlatforms();
    loadNonSteamApps();
  };

  let platformsBody: ReactNode;
  if (loading) {
    platformsBody = (
      <PanelSectionRow>
        <Spinner />
      </PanelSectionRow>
    );
  } else if (platforms.length === 0) {
    platformsBody = (
      <PanelSectionRow>
        <Field label="No synced platforms" />
      </PanelSectionRow>
    );
  } else {
    platformsBody = platforms.map((p) => (
      <PanelSectionRow key={p.slug || p.name}>
        <ButtonItem
          layout="below"
          onClick={() => {
            showModal(
              <PlatformActionModal
                platform={p}
                onRemoveShortcuts={() => handleRemoveShortcuts(p)}
                onDeleteSaves={() => handleDeleteSaves(p)}
                onDeleteBios={() => handleDeleteBios(p)}
              />
            );
          }}
        >
          {p.name} ({p.count})
        </ButtonItem>
      </PanelSectionRow>
    ));
  }

  return (
    <>
      <PanelSection title="Remove Shortcuts">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleRemoveAllRomm}>
            {confirmRemoveAllRomm
              ? <span style={{ color: "#ff8800" }}>Confirm: remove all RomM shortcuts?</span>
              : "Remove All RomM Shortcuts"}
          </ButtonItem>
        </PanelSectionRow>
        {status && (
          <PanelSectionRow>
            <Field label={status} />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Per-Platform Actions">
        {platformsBody}
        {actionStatus && (
          <PanelSectionRow>
            <Field label={actionStatus} />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Installed ROMs">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleUninstallAll}>
            {confirmUninstall
              ? <span style={{ color: "#ff8800" }}>Confirm: delete all ROM files?</span>
              : "Uninstall All Installed ROMs"}
          </ButtonItem>
        </PanelSectionRow>
        {confirmUninstall && (
          <PanelSectionRow>
            <Field label={<span style={{ color: "#ff8800" }}>This will delete all downloaded ROM files. Shortcuts remain so you can re-download later.</span>} />
          </PanelSectionRow>
        )}
        {uninstallStatus && (
          <PanelSectionRow>
            <Field label={uninstallStatus} />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};

interface WhitelistSectionProps {
  nonSteamApps: NonSteamApp[];
  whitelistedIds: Set<number>;
  disabledDefaults: string[];
  customNames: string[];
  settingsLoaded: boolean;
  persistWhitelist: (newDisabled: string[], newCustom: string[]) => void;
  resetRemoveConfirms: () => void;
}

const WhitelistSection: FC<WhitelistSectionProps> = ({
  nonSteamApps,
  whitelistedIds,
  disabledDefaults,
  customNames,
  settingsLoaded,
  persistWhitelist,
  resetRemoveConfirms,
}) => {
  const [showWhitelist, setShowWhitelist] = useState(false);
  const [whitelistSearch, setWhitelistSearch] = useState("");

  const filteredApps = useMemo(
    () => whitelistSearch
      ? nonSteamApps.filter((app) => fuzzyMatch(whitelistSearch, app.name))
      : nonSteamApps,
    [nonSteamApps, whitelistSearch]
  );

  const handleToggle = (app: NonSteamApp, checked: boolean) => {
    const matchingPattern = DEFAULT_WHITELIST_PATTERNS.find(
      (p) => app.name.toLowerCase().includes(p)
    );
    let newDisabled = [...disabledDefaults];
    let newCustom = [...customNames];

    if (checked) {
      if (matchingPattern && disabledDefaults.includes(matchingPattern)) {
        newDisabled = newDisabled.filter((p) => p !== matchingPattern);
      } else if (!matchingPattern) {
        if (!newCustom.includes(app.name)) {
          newCustom.push(app.name);
        }
      }
    } else {
      if (matchingPattern) {
        if (!newDisabled.includes(matchingPattern)) {
          newDisabled.push(matchingPattern);
        }
      }
      newCustom = newCustom.filter((n) => n !== app.name);
    }

    persistWhitelist(newDisabled, newCustom);
    resetRemoveConfirms();
  };

  return (
    <>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            setShowWhitelist(!showWhitelist);
            resetRemoveConfirms();
          }}
        >
          {showWhitelist ? "Hide Whitelist" : `Configure Whitelist (${whitelistedIds.size} protected)`}
        </ButtonItem>
      </PanelSectionRow>

      {showWhitelist && !settingsLoaded && (
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
      )}
      {showWhitelist && settingsLoaded && (
        <>
          <PanelSectionRow>
            <TextField
              label="Search games"
              value={whitelistSearch}
              onChange={(e) => setWhitelistSearch(e?.target?.value ?? "")}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label={`Toggle ON to protect (${filteredApps.length}/${nonSteamApps.length}):`} />
          </PanelSectionRow>
          {filteredApps.map((app) => (
            <PanelSectionRow key={app.appId}>
              <ToggleField
                label={
                  DEFAULT_WHITELIST_PATTERNS.some((p) => app.name.toLowerCase().includes(p))
                    ? `${app.name} (auto)`
                    : app.name
                }
                checked={whitelistedIds.has(app.appId)}
                onChange={(checked: boolean) => handleToggle(app, checked)}
              />
            </PanelSectionRow>
          ))}
        </>
      )}
    </>
  );
};

interface RetroDeckSectionProps {
  nonSteamApps: NonSteamApp[];
  whitelistedIds: Set<number>;
  disabledDefaults: string[];
  customNames: string[];
  settingsLoaded: boolean;
  persistWhitelist: (newDisabled: string[], newCustom: string[]) => void;
  refreshPlatforms: () => Promise<void>;
  loadNonSteamApps: () => void;
  setStatus: (s: string) => void;
}

const RetroDeckSection: FC<RetroDeckSectionProps> = ({
  nonSteamApps,
  whitelistedIds,
  disabledDefaults,
  customNames,
  settingsLoaded,
  persistWhitelist,
  refreshPlatforms,
  loadNonSteamApps,
  setStatus,
}) => {
  const [confirmRemoveAll, setConfirmRemoveAll] = useState(false);
  const [confirmRetrodeck, setConfirmRetrodeck] = useState(false);

  const resetRemoveConfirms = () => {
    setConfirmRemoveAll(false);
    setConfirmRetrodeck(false);
  };

  const retrodeckAtRisk = nonSteamApps.some(
    (a) => !whitelistedIds.has(a.appId) && a.name.toLowerCase().includes("retrodeck")
  );

  const handleRemoveAll = async () => {
    if (!confirmRemoveAll) {
      setConfirmRemoveAll(true);
      return;
    }
    if (retrodeckAtRisk && !confirmRetrodeck) {
      setConfirmRetrodeck(true);
      return;
    }
    const toRemove = nonSteamApps.filter((a) => !whitelistedIds.has(a.appId));
    setStatus(`Removing ${toRemove.length} non-steam games...`);
    for (const app of toRemove) {
      SteamClient.Apps.RemoveShortcut(app.appId);
    }
    setStatus(`Removed ${toRemove.length} non-steam game${toRemove.length === 1 ? "" : "s"}`);
    setConfirmRemoveAll(false);
    setConfirmRetrodeck(false);
    loadNonSteamApps();
    refreshPlatforms();
  };

  const removeButtonLabel = () => {
    if (confirmRetrodeck) {
      return <span style={{ color: "#ff4444", fontWeight: "bold" }}>!! RETRODECK WILL BE REMOVED !! Click to confirm</span>;
    }
    if (confirmRemoveAll) {
      if (retrodeckAtRisk) {
        return <span style={{ color: "#ff8800" }}>WARNING: RetroDECK not protected! Remove {nonSteamApps.length - whitelistedIds.size} games?</span>;
      }
      return `Are you sure? Remove ${nonSteamApps.length - whitelistedIds.size} games (${whitelistedIds.size} whitelisted)?`;
    }
    const remaining = nonSteamApps.length - whitelistedIds.size;
    const excluded = whitelistedIds.size > 0 ? ` (${whitelistedIds.size} excluded)` : "";
    return `Remove ${remaining} Non-Steam Games${excluded}`;
  };

  return (
    <PanelSection title="Remove Non-Steam Games">
      {nonSteamApps.length === 0 ? (
        <PanelSectionRow>
          <Field label="No non-steam games found" />
        </PanelSectionRow>
      ) : (
        <>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleRemoveAll}>
              {removeButtonLabel()}
            </ButtonItem>
          </PanelSectionRow>
          {confirmRetrodeck && (
            <PanelSectionRow>
              <Field label={<span style={{ color: "#ff4444" }}>RetroDECK is NOT in the whitelist and will be permanently removed!</span>} />
            </PanelSectionRow>
          )}
          <WhitelistSection
            nonSteamApps={nonSteamApps}
            whitelistedIds={whitelistedIds}
            disabledDefaults={disabledDefaults}
            customNames={customNames}
            settingsLoaded={settingsLoaded}
            persistWhitelist={persistWhitelist}
            resetRemoveConfirms={resetRemoveConfirms}
          />
        </>
      )}
    </PanelSection>
  );
};

interface DangerZoneProps {
  onBack: () => void;
}

export const DangerZone: FC<DangerZoneProps> = ({ onBack }) => {
  const [status, setStatus] = useState("");
  const [platforms, setPlatforms] = useState<RegistryPlatform[]>([]);
  const [loading, setLoading] = useState(true);
  const [disabledDefaults, setDisabledDefaults] = useState<string[]>([]);
  const [customNames, setCustomNames] = useState<string[]>([]);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [nonSteamApps, setNonSteamApps] = useState<NonSteamApp[]>([]);

  const activeDefaults = useMemo(
    () => DEFAULT_WHITELIST_PATTERNS.filter((p) => !disabledDefaults.includes(p)),
    [disabledDefaults]
  );

  const whitelistedIds = useMemo(() => {
    const set = new Set<number>();
    for (const app of nonSteamApps) {
      const lower = app.name.toLowerCase();
      const matchesDefault = activeDefaults.some((p) => lower.includes(p));
      if (matchesDefault || customNames.includes(app.name)) {
        set.add(app.appId);
      }
    }
    return set;
  }, [nonSteamApps, activeDefaults, customNames]);

  const refreshPlatforms = async () => {
    setLoading(true);
    try {
      const result = await getRegistryPlatforms();
      setPlatforms(result.platforms || []);
    } catch {
      setPlatforms([]);
    }
    setLoading(false);
  };

  const loadNonSteamApps = () => {
    const apps: NonSteamApp[] = [];
    try {
      if (typeof collectionStore === "undefined") {
        logWarn("collectionStore not available");
        setNonSteamApps([]);
        return;
      }
      const deckApps = collectionStore.deckDesktopApps?.apps;
      if (!deckApps) {
        logWarn("deckDesktopApps.apps not available");
        setNonSteamApps([]);
        return;
      }
      logInfo(`deckDesktopApps.apps size: ${deckApps.size}`);
      const appIds = Array.from(deckApps.keys());
      for (const appId of appIds) {
        let name = `Unknown (${appId})`;
        if (typeof appStore !== "undefined") {
          const overview = appStore.GetAppOverviewByAppID(appId);
          if (overview) {
            name = overview.strDisplayName || overview.display_name || name;
          }
        }
        apps.push({ appId, name });
      }
    } catch (e) {
      logError(`Failed to enumerate non-steam games: ${e}`);
    }
    apps.sort((a, b) => a.name.localeCompare(b.name));
    setNonSteamApps(apps);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async data loads on mount are the standard React pattern; the rule is overzealous here
    refreshPlatforms();
    loadNonSteamApps();
    getWhitelistSettings().then((s) => {
      setDisabledDefaults(s.disabled_defaults);
      setCustomNames(s.custom_names);
      setSettingsLoaded(true);
    });
  }, []);

  const persistWhitelist = (newDisabled: string[], newCustom: string[]) => {
    setDisabledDefaults(newDisabled);
    setCustomNames(newCustom);
    updateWhitelistSettings(newDisabled, newCustom);
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={onBack}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
          >
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <ShortcutRemovalSection
        platforms={platforms}
        loading={loading}
        refreshPlatforms={refreshPlatforms}
        loadNonSteamApps={loadNonSteamApps}
        status={status}
        setStatus={setStatus}
      />

      <RetroDeckSection
        nonSteamApps={nonSteamApps}
        whitelistedIds={whitelistedIds}
        disabledDefaults={disabledDefaults}
        customNames={customNames}
        settingsLoaded={settingsLoaded}
        persistWhitelist={persistWhitelist}
        refreshPlatforms={refreshPlatforms}
        loadNonSteamApps={loadNonSteamApps}
        setStatus={setStatus}
      />
    </>
  );
};
