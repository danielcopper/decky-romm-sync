/**
 * Library-wide sync preferences. Currently houses the preferred-region
 * dropdown (ADR-0021 §3): which region wins when a game has several dumps
 * (versions) and the plugin must pick one to bind and to name the Steam
 * shortcut after. Pure renderer: parent owns the current value, the list of
 * regions discovered in the local library, and the save/confirm flow.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, DropdownItem } from "@decky/ui";

interface LibrarySectionProps {
  preferredRegion: string;
  // Distinct region values found in the locally synced library (from the
  // backend get_known_regions read). Appended after the fixed anchors.
  libraryRegions: string[];
  onPreferredRegionChange: (region: string) => void;
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
    </PanelSection>
  );
};
