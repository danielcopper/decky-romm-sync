import { useState, useEffect, useMemo, useRef, FC } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, ToggleField, DialogButton, Field, Focusable } from "@decky/ui";
import {
  getPlatforms,
  savePlatformSync,
  setAllPlatformsSync,
  getCollections,
  saveCollectionSync,
  setAllCollectionsSync,
  saveCollectionPlatformGroups,
  setCollectionOwnerScope,
  getSettings,
} from "../api/backend";
import type {
  PlatformSyncSetting,
  CollectionSyncSetting,
  CollectionKind,
  CollectionScope,
  CollectionOwnerScope,
  VirtualCollectionType,
} from "../types";
import { scrollToTop } from "../utils/scrollHelpers";
import { detach } from "../utils/detach";
import { LoadingRow } from "./LoadingRow";

type CollectionSubTab = "user" | "smart" | "virtual";

const SUB_TAB_ORDER: readonly CollectionSubTab[] = ["user", "smart", "virtual"];

const SUB_TAB_LABELS: Record<CollectionSubTab, string> = {
  user: "My",
  smart: "Smart",
  virtual: "Virtual",
};

const SUB_TAB_HEADERS: Record<CollectionSubTab, string> = {
  user: "MY COLLECTIONS",
  smart: "SMART COLLECTIONS",
  virtual: "VIRTUAL",
};

// Row-description label per virtual type. "IGDB Collection" (not "Collection")
// disambiguates from the Collections page it lives inside — RomM's own label.
const VIRTUAL_TYPE_LABELS: Record<VirtualCollectionType, string> = {
  franchise: "Franchise",
  collection: "IGDB Collection",
};

// A virtual row shows its type ("Franchise" / "IGDB Collection") before the ROM
// count; user/smart rows (and a virtual row missing its type on an older
// backend) show the plain count.
function collectionRowDescription(c: CollectionSyncSetting): string {
  if (c.kind === "virtual" && c.virtual_type) {
    return `${VIRTUAL_TYPE_LABELS[c.virtual_type]} · ${c.rom_count} ROMs`;
  }
  return `${c.rom_count} ROMs`;
}

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
    case "user":
      return collections.filter((c) => c.kind === "user" && (includeFavoritesInMy || !c.is_favorite));
    case "smart":
      return collections.filter((c) => c.kind === "smart");
    case "virtual":
      return collections.filter((c) => c.kind === "virtual");
  }
}

// Owner-scope filter (#1532): under "Own", hide collections owned by another
// user so foreign ones can't be toggled on while the scope excludes them from
// the sync. An absent `is_own` (older backend, or unknown own identity) is
// treated as own, so "Own" never hides a collection it can't classify —
// matching the backend's degrade-to-"All" fallback.
function filterCollectionsByOwnerScope(
  collections: CollectionSyncSetting[],
  ownerScope: CollectionOwnerScope,
): CollectionSyncSetting[] {
  if (ownerScope === "all") return collections;
  return collections.filter((c) => c.is_own !== false);
}

function favoritesDescription(romCount: number): string {
  if (romCount === 1) return "Includes 1 favorited ROM";
  return `Includes ${romCount} favorited ROMs`;
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
  const [ownerScope, setOwnerScope] = useState<CollectionOwnerScope>("all");
  const [activeSubTab, setActiveSubTab] = useState<CollectionSubTab>("user");

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
  // Sub-tab is reset to "user" in the tab-click handler (not here);
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
          setOwnerScope(settingsResult.collection_owner_scope === "own" ? "own" : "all");
        })
        .catch(() => setCollectionsError(true))
        .finally(() => setCollectionsLoading(false));
    }
  }, [activeTab]);

  // Reset the collections sub-tab on every entry into the Collections tab
  // so the user lands on a predictable view (no persistence).
  const handleCollectionsTabClick = () => {
    setActiveSubTab("user");
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

  const handleOwnerScopeChange = async (scope: CollectionOwnerScope) => {
    if (scope === ownerScope) return;
    const previous = ownerScope;
    setOwnerScope(scope);
    try {
      await setCollectionOwnerScope(scope);
    } catch {
      setOwnerScope(previous);
    }
  };

  // --- Platforms tab content ---
  const renderPlatformsContent = () => {
    if (syncLoading) {
      return <LoadingRow />;
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
              // Prefer the persisted post-collapse shortcut count (#1382) — the
              // number of games the platform actually syncs into Steam — over
              // the raw server file count; raw is the never-synced fallback.
              description={`${platform.collapsed_count ?? platform.rom_count} ROMs`}
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
          <LoadingRow />
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
    const kindFiltered = filterCollectionsBySubTab(collections, activeSubTab, includeFavoritesInMy);
    // Owner-scope filter runs OVER the kind sub-tab filter — under "Own" a
    // foreign collection is hidden from every kind tab (#1532).
    const visible = filterCollectionsByOwnerScope(kindFiltered, ownerScope);
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
        <PanelSection>
          <PanelSectionRow>
            <Field
              label="Show collections"
              description={
                ownerScope === "own"
                  ? "Only your own collections (virtual collections always sync)."
                  : "Every collection on the server, including other users' public ones."
              }
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px" }}>
              {(["all", "own"] as CollectionOwnerScope[]).map((scope) => (
                <DialogButton
                  key={scope}
                  style={{
                    flex: 1,
                    minWidth: 0,
                    padding: "8px 0",
                    opacity: ownerScope === scope ? 1 : 0.5,
                    borderBottom: ownerScope === scope ? "2px solid #1a9fff" : "2px solid transparent",
                  }}
                  onClick={() => {
                    detach(handleOwnerScopeChange(scope));
                  }}
                >
                  {scope === "own" ? "Own" : "All"}
                </DialogButton>
              ))}
            </Focusable>
          </PanelSectionRow>
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
                  description={collectionRowDescription(collection)}
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
