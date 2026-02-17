import { useState, useEffect, useMemo, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  TextField,
  ToggleField,
  Spinner,
} from "@decky/ui";
import {
  getRegistryPlatforms,
  removePlatformShortcuts,
  removeAllShortcuts,
  reportRemovalResults,
  uninstallAllRoms,
} from "../api/backend";
import { removeShortcut } from "../utils/steamShortcuts";
import { clearPlatformCollection, clearAllRomMCollections } from "../utils/collections";
import type { RegistryPlatform } from "../types";

interface DangerZoneProps {
  onBack: () => void;
}

export const DangerZone: FC<DangerZoneProps> = ({ onBack }) => {
  const [status, setStatus] = useState("");
  const [platforms, setPlatforms] = useState<RegistryPlatform[]>([]);
  const [loading, setLoading] = useState(true);
  const [showWhitelist, setShowWhitelist] = useState(false);
  const [whitelist, setWhitelist] = useState<Set<number>>(new Set());
  const [nonSteamApps, setNonSteamApps] = useState<{ appId: number; name: string }[]>([]);
  const [confirmRemoveAllRomm, setConfirmRemoveAllRomm] = useState(false);
  const [confirmPlatformSlug, setConfirmPlatformSlug] = useState<string | null>(null);
  const [confirmRemoveAll, setConfirmRemoveAll] = useState(false);
  const [confirmRetrodeck, setConfirmRetrodeck] = useState(false);
  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [uninstallStatus, setUninstallStatus] = useState("");
  const [whitelistSearch, setWhitelistSearch] = useState("");

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
    const apps: { appId: number; name: string }[] = [];
    try {
      if (typeof collectionStore === "undefined") {
        console.warn("[RomM] collectionStore not available");
        setNonSteamApps([]);
        return;
      }
      const deckApps = collectionStore.deckDesktopApps?.apps;
      if (!deckApps) {
        console.warn("[RomM] deckDesktopApps.apps not available");
        setNonSteamApps([]);
        return;
      }
      console.log("[RomM] deckDesktopApps.apps size:", deckApps.size);
      let appIds = Array.from(deckApps.keys());
      const autoWhitelist = new Set<number>();
      for (const appId of appIds) {
        let name = `Unknown (${appId})`;
        if (typeof appStore !== "undefined") {
          const overview = appStore.GetAppOverviewByAppID(appId);
          if (overview) {
            name = (overview as any).strDisplayName || (overview as any).display_name || name;
          }
        }
        apps.push({ appId, name });
        // Auto-whitelist RetroDECK
        if (name.toLowerCase().includes("retrodeck")) {
          autoWhitelist.add(appId);
        }
      }
      if (autoWhitelist.size > 0) {
        setWhitelist((prev) => {
          const next = new Set(prev);
          for (const id of autoWhitelist) next.add(id);
          return next;
        });
      }
    } catch (e) {
      console.error("[RomM] Failed to enumerate non-steam games:", e);
    }
    apps.sort((a, b) => a.name.localeCompare(b.name));
    setNonSteamApps(apps);
  };

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

  const filteredApps = useMemo(
    () => whitelistSearch
      ? nonSteamApps.filter((app) => fuzzyMatch(whitelistSearch, app.name))
      : nonSteamApps,
    [nonSteamApps, whitelistSearch]
  );

  useEffect(() => {
    refreshPlatforms();
    loadNonSteamApps();
  }, []);

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Remove by Platform">
        {loading ? (
          <PanelSectionRow>
            <Spinner />
          </PanelSectionRow>
        ) : platforms.length === 0 ? (
          <PanelSectionRow>
            <Field label="No synced platforms" />
          </PanelSectionRow>
        ) : (
          platforms.map((p) => (
            <PanelSectionRow key={p.slug || p.name}>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  if (confirmPlatformSlug !== p.slug) {
                    setConfirmPlatformSlug(p.slug);
                    return;
                  }
                  setConfirmPlatformSlug(null);
                  setStatus(`Removing ${p.name}...`);
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
                  setStatus(`Removed ${p.count} ${p.name} game${p.count !== 1 ? "s" : ""}`);
                  await refreshPlatforms();
                  loadNonSteamApps();
                }}
              >
                {confirmPlatformSlug === p.slug
                  ? <span style={{ color: "#ff8800" }}>Confirm: remove {p.count} {p.name} game{p.count !== 1 ? "s" : ""}?</span>
                  : `${p.name} (${p.count})`}
              </ButtonItem>
            </PanelSectionRow>
          ))
        )}
      </PanelSection>

      <PanelSection title="Remove All RomM Games">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
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
            }}
          >
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

      <PanelSection title="Installed ROMs">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              if (!confirmUninstall) {
                setConfirmUninstall(true);
                return;
              }
              try {
                setUninstallStatus("Uninstalling...");
                const result = await uninstallAllRoms();
                setUninstallStatus(result.message);
              } catch {
                setUninstallStatus("Failed to uninstall ROMs");
              }
              setConfirmUninstall(false);
              await refreshPlatforms();
              loadNonSteamApps();
            }}
          >
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

      <PanelSection title="Remove Non-Steam Games">
        {nonSteamApps.length === 0 ? (
          <PanelSectionRow>
            <Field label="No non-steam games found" />
          </PanelSectionRow>
        ) : (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  const retrodeckAtRisk = nonSteamApps.some(
                    (a) => !whitelist.has(a.appId) && a.name.toLowerCase().includes("retrodeck")
                  );
                  if (!confirmRemoveAll) {
                    setConfirmRemoveAll(true);
                    return;
                  }
                  if (retrodeckAtRisk && !confirmRetrodeck) {
                    setConfirmRetrodeck(true);
                    return;
                  }
                  const toRemove = nonSteamApps.filter((a) => !whitelist.has(a.appId));
                  setStatus(`Removing ${toRemove.length} non-steam games...`);
                  for (const app of toRemove) {
                    SteamClient.Apps.RemoveShortcut(app.appId);
                  }
                  setStatus(`Removed ${toRemove.length} non-steam game${toRemove.length !== 1 ? "s" : ""}`);
                  setConfirmRemoveAll(false);
                  setConfirmRetrodeck(false);
                  loadNonSteamApps();
                  refreshPlatforms();
                }}
              >
                {confirmRetrodeck
                  ? <span style={{ color: "#ff4444", fontWeight: "bold" }}>!! RETRODECK WILL BE REMOVED !! Click to confirm</span>
                  : confirmRemoveAll
                    ? nonSteamApps.some((a) => !whitelist.has(a.appId) && a.name.toLowerCase().includes("retrodeck"))
                      ? <span style={{ color: "#ff8800" }}>WARNING: RetroDECK not protected! Remove {nonSteamApps.length - whitelist.size} games?</span>
                      : `Are you sure? Remove ${nonSteamApps.length - whitelist.size} games (${whitelist.size} whitelisted)?`
                    : `Remove ${nonSteamApps.length - whitelist.size} Non-Steam Games${whitelist.size > 0 ? ` (${whitelist.size} excluded)` : ""}`}
              </ButtonItem>
            </PanelSectionRow>
            {confirmRetrodeck && (
              <PanelSectionRow>
                <Field label={<span style={{ color: "#ff4444" }}>RetroDECK is NOT in the whitelist and will be permanently removed!</span>} />
              </PanelSectionRow>
            )}
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => {
                  setShowWhitelist(!showWhitelist);
                  setConfirmRemoveAll(false);
                }}
              >
                {showWhitelist ? "Hide Whitelist" : `Configure Whitelist (${whitelist.size} protected)`}
              </ButtonItem>
            </PanelSectionRow>

            {showWhitelist && (
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
                      label={app.name}
                      checked={whitelist.has(app.appId)}
                      onChange={(checked: boolean) => {
                        setWhitelist((prev) => {
                          const next = new Set(prev);
                          if (checked) {
                            next.add(app.appId);
                          } else {
                            next.delete(app.appId);
                          }
                          return next;
                        });
                        setConfirmRemoveAll(false);
                        setConfirmRetrodeck(false);
                      }}
                    />
                  </PanelSectionRow>
                ))}
              </>
            )}
          </>
        )}
      </PanelSection>
    </>
  );
};
