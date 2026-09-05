/**
 * The Library page's Platforms tab: every platform RomM reports with at least
 * one ROM on the left, the focused one's detail on the right.
 *
 * The row is where a platform is switched on and off. That is the page's only
 * sync control: focus is already on the row and A works the toggle, so the
 * detail does not offer a second one.
 *
 * The BIOS state is a DOT and nothing else. The row carried the ratio too until
 * the device pass found it earned nothing in a line you scan past — the detail
 * pane one keypress away states it properly, with the files it is made of. So
 * the dot now carries the signal alone rather than reinforcing a number: it
 * takes the shared mapping's answer, it is drawn on every row even where there
 * is no level (one that came and went shifted every name beside it), and the
 * words behind it are the row's `title`, which is where the number went rather
 * than away.
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

/**
 * What the row's dot means, in words — its `title`, and the only place the list
 * still states the number.
 *
 * The wording is the detail pane's own, so a reader who hovers here and then
 * opens the pane meets the same vocabulary rather than two names for one state.
 */
function biosTooltip(row: PlatformRow): string {
  const firmware = row.firmware;
  if (!firmware) return "Nothing is known about this platform's BIOS files";
  if (firmware.bios_level === "unknown") {
    return (firmware.required_withheld ?? 0) > 0 ? "BIOS readiness unknown" : "BIOS requirement unknown";
  }
  const required = firmware.required_count ?? 0;
  if (required === 0) return "Nothing required";
  return `${firmware.required_downloaded ?? 0} / ${required} required BIOS files ready`;
}

// The selection marker and the gap after it, which together are how far a row's
// content sits from the list column's edge. Named because the list header's
// padding has to be the same number: the pair of buttons up there spans the rows
// below it, and happy-dom lays nothing out, so a drift between the two would be
// invisible to every test.
const ROW_MARKER_WIDTH = 3;
const ROW_MARKER_GAP = 5;
const ROW_CONTENT_INSET = ROW_MARKER_WIDTH + ROW_MARKER_GAP;

const GroupHeading: FC<{ title: string; count: number }> = ({ title, count }) => (
  // Plain text: it accompanies the rows under it and scrolls with them. Making
  // it a focus stop would put a step between two rows that leads nowhere.
  //
  // Flush left, with the rows indented past it — a heading further right than
  // what it heads reads as a child of the row above. Uppercase to match the
  // detail's own section titles, which are the only other labels of this kind.
  <div
    style={{
      padding: "8px 8px 2px",
      fontSize: "11px",
      fontWeight: 600,
      letterSpacing: "0.5px",
      color: "#8f98a0",
    }}
  >
    {title.toUpperCase()} ({count})
  </div>
);

const RowLabel: FC<{ row: PlatformRow; selected: boolean }> = ({ row, selected }) => {
  const level = row.firmware?.bios_level ?? null;
  return (
    <span style={{ display: "flex", alignItems: "center", gap: "6px", minWidth: 0 }} title={biosTooltip(row)}>
      {/* Always drawn, grey where there is no level to state: a dot that comes
          and goes shifts every name beside it, and the list is meant to be
          scanned down its left edge. */}
      <span
        data-testid={`bios-dot-${row.slug}`}
        style={{
          display: "inline-block",
          width: "8px",
          height: "8px",
          borderRadius: "50%",
          backgroundColor: biosColorForLevel(level),
          flexShrink: 0,
        }}
      />
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
            {/* The marker bar, with room between it and the status dot — flush
                against the dot it reads as part of it. */}
            <div
              style={{
                borderLeft: `${ROW_MARKER_WIDTH}px solid ${selected ? "#1a9fff" : "transparent"}`,
                paddingLeft: `${ROW_MARKER_GAP}px`,
              }}
            >
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
    <>
      {/* The pair spans exactly what a row below it spans, which is NOT
          symmetric: a row is inset on the left by its own marker bar and the gap
          after it, and runs flush to the column's right edge. Steam's `Field`
          contributes nothing horizontally to that — inside the QAM it renders in
          its `Classic` mode, whose only padding is 10px top and bottom — so
          there is no Field inset to match, and a right padding here would be
          that much of the rows' width the buttons do not have. Measured on the
          device through CEF: rows 79.6 → 335.9, buttons 79.9 → 335.9. */}
      <Focusable
        flow-children="horizontal"
        style={{ display: "flex", gap: "8px", padding: `4px 0 8px ${ROW_CONTENT_INSET}px` }}
      >
        <DialogButton style={{ flex: 1, minWidth: 0, padding: "8px 0" }} onClick={() => state.setAllSync(true)}>
          Enable all
        </DialogButton>
        <DialogButton style={{ flex: 1, minWidth: 0, padding: "8px 0" }} onClick={() => state.setAllSync(false)}>
          Disable all
        </DialogButton>
      </Focusable>
      {/* Why a sync write did not take, under the buttons and inset with them:
          the writes are optimistic, so a refusal puts the row back, and a revert
          with nothing said is what a toggle that never moved looks like. Plain
          text for the reason the group headings are plain text — a focus stop
          here would sit between the buttons and the first row and lead nowhere —
          and it belongs to the list rather than to a platform's pane, because
          Enable all is about every row at once. */}
      {state.listStatus && (
        <div
          data-testid="status-list"
          style={{ fontSize: "12px", color: "#dcdedf", padding: `0 0 8px ${ROW_CONTENT_INSET}px` }}
        >
          {state.listStatus}
        </div>
      )}
    </>
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
