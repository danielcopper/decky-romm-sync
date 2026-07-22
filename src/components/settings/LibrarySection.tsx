/**
 * Library-wide sync preferences. Houses set-and-forget library toggles: the
 * preferred-region dropdown (ADR-0021 §3, which region wins when a game has
 * several dumps and the plugin must pick one to bind + name the shortcut after)
 * and the collection platform-groups toggle. Pure renderer: parent owns every
 * value and the save/confirm flow.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, DropdownItem, ToggleField } from "@decky/ui";
import type { CollectionNamingMode } from "../../types";

interface LibrarySectionProps {
  preferredRegion: string;
  // Distinct region values found in the locally synced library (from the
  // backend get_known_regions read). Appended after the fixed anchors.
  libraryRegions: string[];
  onPreferredRegionChange: (region: string) => void;
  // Whether a synced collection's games are also added to their platform's
  // Steam group. A set-and-forget library preference, so it lives here rather
  // than on the per-sync Collections tab.
  platformGroups: boolean;
  onPlatformGroupsChange: (enabled: boolean) => void;
  // Steam-collection naming mode (#1539). "by_label" appends the fine type
  // label to the Steam collection name so same-named collections of different
  // types stay separate; "merge" (default) unions them. Rendered as a boolean
  // toggle (checked === "by_label").
  namingMode: CollectionNamingMode;
  onNamingModeChange: (mode: CollectionNamingMode) => void;
}

// The internal sentinel for "no preference — use the fixed build-time order".
export const AUTO_REGION = "auto";

// The fixed anchor regions, in the build-time default order. MIRRORS the backend
// constant DEFAULT_REGION_PRIORITY (py_modules/domain/sibling_resolution.py) —
// keep the two in sync. This is a fixed order, NOT language/system detection.
export const ANCHOR_REGIONS: readonly string[] = ["World", "USA", "Europe", "Japan"];

// The default option's label states the order explicitly so it never reads as
// auto-detection.
export const DEFAULT_REGION_LABEL = "Default (World > USA > Europe)";

/**
 * Build the dropdown options: the "Default" sentinel + the fixed anchors, then
 * every OTHER region found in the local library (sorted, de-duped against the
 * anchors). The currently-selected value is always included so a preference for
 * a region no longer in the library still renders as selected.
 */
export function buildRegionOptions(libraryRegions: string[], selected: string): { data: string; label: string }[] {
  const options: { data: string; label: string }[] = [
    { data: AUTO_REGION, label: DEFAULT_REGION_LABEL },
    ...ANCHOR_REGIONS.map((r) => ({ data: r, label: r })),
  ];
  const known = new Set<string>([AUTO_REGION, ...ANCHOR_REGIONS]);
  const extras = Array.from(new Set(libraryRegions))
    .filter((r) => r && !known.has(r))
    .sort((a, b) => a.localeCompare(b));
  for (const r of extras) {
    options.push({ data: r, label: r });
    known.add(r);
  }
  if (selected !== AUTO_REGION && !known.has(selected)) {
    options.push({ data: selected, label: selected });
  }
  return options;
}

export const LibrarySection: FC<LibrarySectionProps> = ({
  preferredRegion,
  libraryRegions,
  onPreferredRegionChange,
  platformGroups,
  onPlatformGroupsChange,
  namingMode,
  onNamingModeChange,
}) => {
  return (
    <PanelSection title="Library">
      <PanelSectionRow>
        <DropdownItem
          label="Preferred region"
          description="When a game has several regional versions, prefer this region for the shortcut and its name. Applies to games synced from now on; existing shortcuts keep their name."
          rgOptions={buildRegionOptions(libraryRegions, preferredRegion)}
          selectedOption={preferredRegion}
          onChange={(option) => onPreferredRegionChange(option.data)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="Show collection games in platform groups"
          description="When syncing a collection, also add its games to their platform-specific Steam group."
          checked={platformGroups}
          onChange={onPlatformGroupsChange}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="Distinguish collection types in Steam names"
          description="Adds the collection type (e.g. Franchise, IGDB Collection) to the Steam collection name so collections that share a name stay separate instead of merging into one. Applies on the next sync."
          checked={namingMode === "by_label"}
          onChange={(v) => onNamingModeChange(v ? "by_label" : "merge")}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
