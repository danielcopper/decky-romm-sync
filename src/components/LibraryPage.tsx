import { useState, useEffect, useMemo, useRef, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ToggleField,
  Spinner,
  DialogButton,
  Field,
  Focusable,
} from "@decky/ui";
import {
  getPlatforms,
  savePlatformSync,
  setAllPlatformsSync,
  getCollections,
  saveCollectionSync,
  setAllCollectionsSync,
  saveCollectionPlatformGroups,
  getSettings,
} from "../api/backend";
import type { PlatformSyncSetting, CollectionSyncSetting, CollectionKind, CollectionScope } from "../types";
import { scrollToTop } from "../utils/scrollHelpers";
import { detach } from "../utils/detach";

type CollectionSubTab = "my" | "smart" | "franchise";

const SUB_TAB_ORDER: readonly CollectionSubTab[] = ["my", "smart", "franchise"];

const SUB_TAB_LABELS: Record<CollectionSubTab, string> = {
  my: "My",
  smart: "Smart",
  franchise: "Franchise",
};

const SUB_TAB_HEADERS: Record<CollectionSubTab, string> = {
  my: "MY COLLECTIONS",
  smart: "SMART COLLECTIONS",
  franchise: "FRANCHISE",
};

function filterCollectionsBySubTab(
  collections: CollectionSyncSetting[],
  subTab: CollectionSubTab,
  // When the favorites toggle isn't shown (zero or >1 favorites), the "My"
  // sub-tab includes favorites too so they remain reachable. Defaults to
  // false because the optimistic-update callsite in handleSetAllCollections
  // doesn't care — it only ever inspects the favorites-excluded "My" set,
  // and the favorites toggle owns favorites mutations independently.
  includeFavoritesInMy = false,
): CollectionSyncSetting[] {
  switch (subTab) {
    case "my":
      return collections.filter((c) => c.kind === "user" && (includeFavoritesInMy || !c.is_favorite));
    case "smart":
      return collections.filter((c) => c.kind === "smart");
    case "franchise":
      return collections.filter((c) => c.kind === "franchise");
  }
}

function favoritesDescription(romCount: number): string {
  if (romCount === 1) return "Includes 1 favorited game";
  return `Includes ${romCount} favorited games`;
}

interface LibraryPageProps {
  onBack: () => void;
}

export const LibraryPage: FC<LibraryPageProps> = ({ onBack }) => {
  const [activeTab, setActiveTab] = useState<"platforms" | "collections">("platforms");

  // --- Platforms tab state ---
  const [syncPlatforms, setSyncPlatforms] = useState<PlatformSyncSetting[]>([]);
  const [syncLoading, setSyncLoading] = useState(true);
  const [syncError, setSyncError] = useState(false);

  // --- Collections tab state ---
  const [collections, setCollections] = useState<CollectionSyncSetting[]>([]);
  const [collectionsLoading, setCollectionsLoading] = useState(true);
  const [collectionsError, setCollectionsError] = useState(false);
  const collectionsLoaded = useRef(false);
  const [platformGroups, setPlatformGroups] = useState(false);
  const [activeSubTab, setActiveSubTab] = useState<CollectionSubTab>("my");

  // The favorites collection (a user collection with is_favorite=true) is
  // promoted to a top-level toggle. RomM's schema theoretically allows more
  // than one — if that ever happens, drop the toggle and let the "My" sub-tab
  // surface them all, since a single toggle can't represent the set.
  const favoritesCollection = useMemo(() => {
    const favs = collections.filter((c) => c.kind === "user" && c.is_favorite);
    if (favs.length === 0) return null;
    if (favs.length > 1) {
      console.warn(
        `decky-romm-sync: expected at most one favorites collection, got ${favs.length}. ` +
          `Falling back to listing them in the My sub-tab.`,
      );
      return null;
    }
    return favs[0] ?? null;
  }, [collections]);

  // Load sync platforms on mount
  useEffect(() => {
    getPlatforms()
      .then((result) => {
        if (result.success) {
          setSyncPlatforms(result.platforms);
        } else {
          setSyncError(true);
        }
      })
      .catch(() => setSyncError(true))
      .finally(() => setSyncLoading(false));
  }, []);

  // Load collections data lazily on first switch to collections tab.
  // Sub-tab is reset to "my" in the tab-click handler (not here);
  // that's an event-driven concern, not state synchronisation.
  useEffect(() => {
    if (activeTab === "collections" && !collectionsLoaded.current) {
      collectionsLoaded.current = true;
      Promise.all([getCollections(), getSettings()])
        .then(([collResult, settingsResult]) => {
          if (collResult.success) {
            setCollections(collResult.collections);
          } else {
            setCollectionsError(true);
          }
          setPlatformGroups(!!settingsResult.collection_create_platform_groups);
        })
        .catch(() => setCollectionsError(true))
        .finally(() => setCollectionsLoading(false));
    }
  }, [activeTab]);

  // Reset the collections sub-tab on every entry into the Collections tab
  // so the user lands on a predictable view (no persistence).
  const handleCollectionsTabClick = () => {
    setActiveSubTab("my");
    setActiveTab("collections");
  };

  // --- Platforms tab handlers ---
  const handleToggle = async (id: number, enabled: boolean) => {
    setSyncPlatforms((prev) => prev.map((p) => (p.id === id ? { ...p, sync_enabled: enabled } : p)));
    try {
      await savePlatformSync(id, enabled);
    } catch {
      setSyncPlatforms((prev) => prev.map((p) => (p.id === id ? { ...p, sync_enabled: !enabled } : p)));
    }
  };

  const handleSetAll = async (enabled: boolean) => {
    const previous = syncPlatforms.map((p) => ({ ...p }));
    setSyncPlatforms((prev) => prev.map((p) => ({ ...p, sync_enabled: enabled })));
    try {
      await setAllPlatformsSync(enabled);
    } catch {
      setSyncPlatforms(previous);
    }
  };

  // --- Collections tab handlers ---
  const handleCollectionToggle = async (id: string, kind: CollectionKind, enabled: boolean) => {
    setCollections((prev) => prev.map((c) => (c.id === id && c.kind === kind ? { ...c, sync_enabled: enabled } : c)));
    try {
      await saveCollectionSync(id, kind, enabled);
    } catch {
      setCollections((prev) =>
        prev.map((c) => (c.id === id && c.kind === kind ? { ...c, sync_enabled: !enabled } : c)),
      );
    }
  };

  const handleSetAllCollections = async (enabled: boolean, scope: CollectionScope) => {
    const previous = collections.map((c) => ({ ...c }));
    // Optimistically flip only the entries in the active sub-tab.
    setCollections((prev) =>
      prev.map((c) => (filterCollectionsBySubTab([c], scope).length > 0 ? { ...c, sync_enabled: enabled } : c)),
    );
    try {
      await setAllCollectionsSync(enabled, scope);
    } catch {
      setCollections(previous);
    }
  };

  // --- Platforms tab content ---
  const renderPlatformsContent = () => {
    if (syncLoading) {
      return (
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
      );
    }
    if (syncError) {
      return (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Failed to load platforms
          </ButtonItem>
        </PanelSectionRow>
      );
    }
    return (
      <>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              detach(handleSetAll(true));
            }}
          >
            Enable All
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              detach(handleSetAll(false));
            }}
          >
            Disable All
          </ButtonItem>
        </PanelSectionRow>
        {syncPlatforms.map((platform) => (
          <PanelSectionRow key={platform.id}>
            <ToggleField
              label={platform.name}
              description={`${platform.rom_count} ROMs`}
              checked={platform.sync_enabled}
              onChange={(value: boolean) => {
                detach(handleToggle(platform.id, value));
              }}
            />
          </PanelSectionRow>
        ))}
      </>
    );
  };

  // --- Collections tab content ---
  const renderCollectionsContent = () => {
    if (collectionsLoading) {
      return (
        <PanelSection title="Collections">
          <PanelSectionRow>
            <Spinner />
          </PanelSectionRow>
        </PanelSection>
      );
    }
    if (collectionsError) {
      return (
        <PanelSection title="Collections">
          <PanelSectionRow>
            <Field label="Failed to load collections" description="Check your connection and try again" />
          </PanelSectionRow>
        </PanelSection>
      );
    }
    if (collections.length === 0) {
      return (
        <PanelSection title="Collections">
          <PanelSectionRow>
            <Field label="No collections found" description="Create collections in RomM to sync them here" />
          </PanelSectionRow>
        </PanelSection>
      );
    }

    // When the favorites toggle isn't rendered (zero or multi-favorites case),
    // include any favorites in the "My" sub-tab so they stay reachable.
    const includeFavoritesInMy = favoritesCollection === null;
    const visible = filterCollectionsBySubTab(collections, activeSubTab, includeFavoritesInMy);
    const activeLabel = SUB_TAB_LABELS[activeSubTab];
    const sectionTitle = `${SUB_TAB_HEADERS[activeSubTab]} (${visible.length})`;

    return (
      <>
        <PanelSection>
          <PanelSectionRow>
            <ToggleField
              label="Show collection games in platform groups"
              description="When syncing a collection, also add its games to their platform-specific Steam group."
              checked={platformGroups}
              onChange={(value: boolean) => {
                setPlatformGroups(value);
                detach(
                  (async () => {
                    try {
                      await saveCollectionPlatformGroups(value);
                    } catch {
                      setPlatformGroups(!value);
                    }
                  })(),
                );
              }}
            />
          </PanelSectionRow>
          {favoritesCollection && (
            <PanelSectionRow>
              <ToggleField
                label="Sync RomM favorites"
                description={favoritesDescription(favoritesCollection.rom_count)}
                checked={favoritesCollection.sync_enabled}
                onChange={(value: boolean) => {
                  detach(handleCollectionToggle(favoritesCollection.id, favoritesCollection.kind, value));
                }}
              />
            </PanelSectionRow>
          )}
        </PanelSection>
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px", padding: "0 16px 12px" }}>
          {SUB_TAB_ORDER.map((sub) => (
            <DialogButton
              key={sub}
              style={{
                flex: 1,
                minWidth: 0,
                padding: "10px 0",
                opacity: activeSubTab === sub ? 1 : 0.5,
                borderBottom: activeSubTab === sub ? "2px solid #1a9fff" : "2px solid transparent",
              }}
              onClick={() => setActiveSubTab(sub)}
            >
              {SUB_TAB_LABELS[sub]}
            </DialogButton>
          ))}
        </Focusable>
        <PanelSection title={sectionTitle}>
          <PanelSectionRow>
            <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px" }}>
              <DialogButton
                style={{ flex: 1, minWidth: 0 }}
                onClick={() => {
                  detach(handleSetAllCollections(true, activeSubTab));
                }}
              >
                Enable All
              </DialogButton>
              <DialogButton
                style={{ flex: 1, minWidth: 0 }}
                onClick={() => {
                  detach(handleSetAllCollections(false, activeSubTab));
                }}
              >
                Disable All
              </DialogButton>
            </Focusable>
          </PanelSectionRow>
          {visible.length === 0 ? (
            <PanelSectionRow>
              <Field label={`No ${activeLabel.toLowerCase()} collections`} />
            </PanelSectionRow>
          ) : (
            visible.map((collection) => (
              <PanelSectionRow key={`${collection.kind}:${collection.id}`}>
                <ToggleField
                  label={collection.name}
                  description={`${collection.rom_count} ROMs`}
                  checked={collection.sync_enabled}
                  onChange={(value: boolean) => {
                    detach(handleCollectionToggle(collection.id, collection.kind, value));
                  }}
                />
              </PanelSectionRow>
            ))
          )}
        </PanelSection>
      </>
    );
  };

  // --- Render ---
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
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px", padding: "0 16px 12px" }}>
        <DialogButton
          style={{
            flex: 1,
            minWidth: 0,
            padding: "10px 0",
            opacity: activeTab === "platforms" ? 1 : 0.5,
            borderBottom: activeTab === "platforms" ? "2px solid #1a9fff" : "2px solid transparent",
          }}
          onClick={() => setActiveTab("platforms")}
        >
          Platforms
        </DialogButton>
        <DialogButton
          style={{
            flex: 1,
            minWidth: 0,
            padding: "10px 0",
            opacity: activeTab === "collections" ? 1 : 0.5,
            borderBottom: activeTab === "collections" ? "2px solid #1a9fff" : "2px solid transparent",
          }}
          onClick={handleCollectionsTabClick}
        >
          Collections
        </DialogButton>
      </Focusable>

      {activeTab === "platforms" && <PanelSection title="Platforms">{renderPlatformsContent()}</PanelSection>}

      {activeTab === "collections" && <>{renderCollectionsContent()}</>}
    </>
  );
};
