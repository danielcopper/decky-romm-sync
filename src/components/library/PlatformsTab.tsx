/**
 * The Library page's Platforms tab: every platform RomM reports with at least
 * one ROM on the left, the focused one's detail on the right.
 *
 * The row is where a platform is switched on and off. That is the page's only
 * sync control: focus is already on the row and A works the toggle, so the
 * detail does not offer a second one. Beside the toggle the row states the BIOS
 * requirement as a NUMBER — the colour of the dot only reinforces it, and a
 * platform the firmware read has nothing to say about shows neither.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`, section Library.
 */

import type { FC, ReactNode } from "react";
import { DialogButton, Focusable, ToggleField } from "@decky/ui";
import { ListDetail, type ListDetailItem } from "../qam/ListDetail";
import { LoadingRow } from "../LoadingRow";
import { biosColorForLevel } from "../../utils/biosColor";
import { PlatformDetail } from "./PlatformDetail";
import type { PlatformRow, PlatformsPageState } from "./usePlatformsPage";

/** The required-files ratio the row carries, or `null` where the platform makes
 *  no claim to state — no firmware entry, or nothing required. The row prints an
 *  em dash for that: "wants none" and "not asked yet" both read as no number. */
function biosRatio(row: PlatformRow): string | null {
  const required = row.firmware?.required_count ?? 0;
  if (!row.firmware || required === 0) return null;
  return `${row.firmware.required_downloaded ?? 0} / ${required}`;
}

const GroupHeading: FC<{ title: string; count: number }> = ({ title, count }) => (
  // Plain text: it accompanies the rows under it and scrolls with them. Making
  // it a focus stop would put a step between two rows that leads nowhere.
  <div style={{ padding: "8px 16px 2px", fontSize: "11px", fontWeight: 600, color: "#8f98a0" }}>
    {title} ({count})
  </div>
);

const RowLabel: FC<{ row: PlatformRow; selected: boolean }> = ({ row, selected }) => {
  const ratio = biosRatio(row);
  return (
    <span style={{ display: "flex", alignItems: "center", gap: "6px", minWidth: 0 }}>
      {row.firmware && (
        <span
          data-testid={`bios-dot-${row.slug}`}
          style={{
            display: "inline-block",
            width: "8px",
            height: "8px",
            borderRadius: "50%",
            backgroundColor: biosColorForLevel(row.firmware.bios_level ?? null),
            flexShrink: 0,
          }}
        />
      )}
      <span
        style={{
          flex: "1 1 auto",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontWeight: selected ? 600 : 400,
        }}
      >
        {row.name}
      </span>
      <span style={{ flexShrink: 0, fontSize: "12px", color: "#8f98a0" }}>{ratio ?? "—"}</span>
    </span>
  );
};

export const PlatformsTab: FC<{ state: PlatformsPageState }> = ({ state }) => {
  if (state.loading) return <LoadingRow />;
  if (state.failed) {
    return (
      <div style={{ padding: "16px", color: "#8f98a0" }}>
        Could not read your platforms. Check the connection to RomM and open the page again.
      </div>
    );
  }

  const groups = state.groups;
  if (!groups || state.rows.size === 0) {
    return <div style={{ padding: "16px", color: "#8f98a0" }}>RomM reports no platform holding any ROM.</div>;
  }

  const items: ListDetailItem[] = [];
  const pushGroup = (slugs: string[], title: string) => {
    slugs.forEach((slug, index) => {
      const row = state.rows.get(slug);
      if (!row) return;
      items.push({
        id: slug,
        render: (selected: boolean) => (
          <>
            {index === 0 && <GroupHeading title={title} count={slugs.length} />}
            <div style={{ borderLeft: selected ? "3px solid #1a9fff" : "3px solid transparent" }}>
              <ToggleField
                label={<RowLabel row={row} selected={selected} />}
                checked={row.syncEnabled}
                bottomSeparator="none"
                onChange={(value: boolean) => state.toggleSync(row, value)}
              />
            </div>
          </>
        ),
      });
    });
  };
  pushGroup(groups.synced, "Synced");
  pushGroup(groups.available, "Available");

  const listHeader: ReactNode = (
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", padding: "4px 16px 8px" }}>
      <DialogButton style={{ flex: 1, minWidth: 0, padding: "8px 0" }} onClick={() => state.setAllSync(true)}>
        Enable all
      </DialogButton>
      <DialogButton style={{ flex: 1, minWidth: 0, padding: "8px 0" }} onClick={() => state.setAllSync(false)}>
        Disable all
      </DialogButton>
    </Focusable>
  );

  return (
    <ListDetail
      items={items}
      listHeader={listHeader}
      selectedId={state.selectedSlug}
      onSelect={state.select}
      renderDetail={(slug) => {
        const row = slug === null ? undefined : state.rows.get(slug);
        if (!row) return <div style={{ padding: "16px", color: "#8f98a0" }}>Pick a platform on the left.</div>;
        return <PlatformDetail row={row} state={state} />;
      }}
    />
  );
};
