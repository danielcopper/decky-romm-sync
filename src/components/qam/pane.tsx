/**
 * The pieces a wide page's detail pane is built from: the type scale it sets
 * secondary lines in, the colours it says things with, the two button shapes,
 * and the small components every pane repeats — a section title, a muted line,
 * a row of buttons, and the two lines that report an action.
 *
 * They are here rather than on a page because the next pane is written against
 * the same scale: a second literal for the same size is how two panes drift
 * apart, and a reader moving between them reads the drift as meaning.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import type { FC, ReactElement, ReactNode } from "react";
import { Focusable } from "@decky/ui";

/** The size every secondary LINE on a pane is set in: a header's counts clause,
 *  the under-row description and note lines, a table cell beside them, the muted
 *  sentences, the table header and the legend. One constant, because the device
 *  pass asked for the cell to match those lines and a second literal is how they
 *  drift apart again. Button labels are not lines and keep their own sizes. */
export const SECONDARY_FONT = "11px";

export const MUTED = "#8f98a0";
export const RED = "#d94126";
export const GREEN = "#5ba32b";
export const AMBER = "#d4a72c";
/** The BIOS table's verdict mark for a file that is present and that the core
 *  the platform launches with does not require — green's quieter twin, so
 *  "there and needed" and "there and spare" are one glance apart rather than one
 *  reading apart. */
export const PALE_GREEN = "#8fc46b";
/** The BIOS table's library mark, deliberately outside the verdict palette's
 *  traffic light: what it reports — that the RomM library does not hold the
 *  file — is not a degree of wrongness. */
export const VIOLET = "#a48fd4";

/**
 * The padding a `DialogButton` is given wherever a pane puts buttons in a row.
 *
 * `ButtonItem` — the full-width control most of the panel uses — takes no style
 * or class of its own: its props are `ItemProps`, which has neither
 * (`@decky/ui/dist/components/Item.d.ts`), so its height is Steam's and cannot
 * be argued with from here. `DialogButton` does take `style`
 * (`DialogButtonProps extends DialogCommonProps`, `Dialog.d.ts`), and is Steam's
 * own button component rather than a lookalike of one.
 *
 * They are the same button, and the difference is the row around it. In
 * `chunk~2dcc5aaf7.js` module 12316, the `forwardRef` decky's prop-list regex
 * matches (`highlightOnFocus` then `childrenContainerWidth`) renders a `Field`
 * whose first child is a second `forwardRef`, and that one renders `o.$n`;
 * module 64608 re-exports `$n` from module 44351, where it is the `forwardRef`
 * whose className is `"DialogButton","_DialogLayout","Secondary"` — the exact
 * string `@decky/ui` searches for to bind its own `DialogButton`
 * (`components/Dialog.js`, `DialogButton = DialogButtonSecondary`). So
 * `ButtonItem` IS a `Field` wrapped around this component, and what a `Field`
 * costs is the row's own padding: 10px top and bottom inside the QAM, where it
 * renders in its `Classic` mode.
 */
export const FLAT_BUTTON = { flex: "1 1 auto", minWidth: 0, padding: "6px 10px", fontSize: "13px" } as const;

/** The button in a table row's action column. Narrow because the column is
 *  sized for it and the name beside it is the thing worth width: 4px of
 *  horizontal padding on the BIOS table's 92px action column still leaves a
 *  target wider than it is tall, which is what keeps it pressable at the Deck's
 *  scale. */
export const ROW_BUTTON = { width: "100%", minWidth: 0, padding: "4px", fontSize: "11px" } as const;

export const SectionTitle: FC<{ title: string; note?: string; noteColor?: string }> = ({ title, note, noteColor }) => (
  <div style={{ display: "flex", alignItems: "baseline", gap: "8px", padding: "12px 16px 4px" }}>
    <span style={{ fontSize: "12px", fontWeight: 600, letterSpacing: "0.5px", color: "#dcdedf" }}>
      {title.toUpperCase()}
    </span>
    {note && <span style={{ fontSize: SECONDARY_FONT, color: noteColor ?? MUTED }}>{note}</span>}
  </div>
);

export const Muted: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ fontSize: SECONDARY_FONT, color: MUTED, padding: "0 16px 6px" }}>{children}</div>
);

/**
 * A row of side-by-side buttons, crossed horizontally by the stick.
 *
 * The padding is the caller's because it is the row's place on the pane rather
 * than the row's own shape — how much air it needs above and below depends on
 * what it sits between.
 */
export const ButtonRow: FC<{ padding: string; children: ReactNode }> = ({ padding, children }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", padding }}>
    {children}
  </Focusable>
);

/**
 * A line reporting what an action did, bound to the entry it was produced for
 * and the group of the pane it belongs under.
 *
 * `key` is the pane's own identity — a platform slug, a section id — and `scope`
 * the group. Both halves matter: a failed core switch must not be reported under
 * Remove, and an action's result must not follow the reader onto the next
 * entry's pane.
 *
 * A page names its own set of groups as `Scope`, so the literals it writes at
 * each call site are checked against that set. Defaulting to `string` keeps a
 * page that has only one group from having to declare one.
 */
export interface ScopedStatus<Scope extends string = string> {
  key: string;
  scope: Scope;
  text: string;
}

/**
 * A generic function rather than an `FC`, because `FC` takes no type parameter.
 *
 * `Scope` is inferred from `status` alone and `scope` is only checked against
 * it, which is what `NoInfer` buys: with both as inference sites a literal
 * naming no group of the page's set is simply a second candidate, `Scope` widens
 * to the union of the two, and the typo passes. Catching it is the whole point
 * of the parameter, because a scope matching nothing renders no line and says
 * nothing about why.
 */
export function GroupStatus<Scope extends string>({
  status,
  forKey,
  scope,
}: {
  status: ScopedStatus<Scope> | null;
  forKey: string;
  scope: NoInfer<Scope>;
}): ReactElement | null {
  return status?.key === forKey && status.scope === scope ? (
    <div data-testid={`status-${scope}`} style={{ fontSize: "12px", color: "#dcdedf", padding: "0 16px 8px" }}>
      {status.text}
    </div>
  ) : null;
}

/**
 * Why this pane's buttons are dead while nothing on it is running.
 *
 * A page that holds one status line, one progress and one busy key runs one
 * action at a time across every entry: a second would clobber the first's line
 * and the first `finally` would clear the busy state under the second. The line
 * that would explain the wait — {@link GroupStatus} — is bound to the entry the
 * action belongs to, so walking away from a running action leaves a pane full of
 * disabled buttons and nothing said. This is what it says.
 *
 * `busyName` is what the page calls the entry that is working, and is required:
 * a page that cannot name it has to choose the words the reader sees rather than
 * inherit them from here.
 */
export const BusyElsewhere: FC<{ busyKey: string | null; ownKey: string; busyName: string }> = ({
  busyKey,
  ownKey,
  busyName,
}) => {
  if (busyKey === null || busyKey === ownKey) return null;
  return <Muted>{`Working on ${busyName} — actions here are paused until it finishes.`}</Muted>;
};
