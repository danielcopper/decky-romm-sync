/**
 * What can hold gamepad focus, where a page's focus lands when it opens, and how
 * it is put there.
 *
 * Steam's gamepad navigation keeps a focus pointer across a page swap and
 * resolves it on the next input — onto whatever sits at the old page's
 * position — so a newly mounted page has to claim focus itself. Two callers do,
 * on the same delay and by different rules: the wide-page frame for its own body
 * (`src/components/qam/WidePage.tsx`), and the panel's router for the narrow
 * pages that place none of their own (`src/index.tsx`).
 */

/**
 * Every shape Steam gives a focus stop, measured in the running QAM rather than
 * assumed: its own components render `div[tabindex="0"]` (`Focusable`, a toggle
 * row, a table row carrying an activate handler) and a `DialogButton` is a
 * native `button` with no tabindex attribute at all.
 *
 * The two selectors below are joined from this one list rather than written out
 * twice, so a shape added here reaches both.
 */
const FOCUS_STOP_SHAPES = ["[tabindex]", "button", "a[href]", "input", "select", "textarea"];

/**
 * Anything focus can land on, disabled controls included.
 *
 * A wide page keeps its buttons rendered and disabled rather than hidden, and
 * the injected sheet gives them Steam's focus outline, so the stick lands on
 * them and the row stays walkable — which is why `ScrollRegion` reads this one,
 * and the entry-focus finder below takes its candidates from the narrower one
 * while testing containment against this.
 */
export const FOCUS_STOPS = FOCUS_STOP_SHAPES.join(", ");

// The same shapes, less anything disabled: what entry focus may land ON.
const ENTRY_STOPS = FOCUS_STOP_SHAPES.map((shape) => `${shape}:not([disabled])`).join(", ");

/**
 * The stop a wide page's body opens on: the first enabled thing in document
 * order that can take focus and contains no focus stop of its own.
 *
 * Document order, never "the first button", because a page's first button is not
 * its first row. On a list-and-detail page whose list rows carry no control of
 * their own, the first button in the body is in the DETAIL pane, so a
 * button-first rule opens the page somewhere inside the detail and moves as the
 * detail's content changes.
 *
 * Innermost, because a container `Focusable` carries `tabindex="0"` of its own
 * and precedes in document order every row it wraps: taking the first match
 * lands focus on the container and leaves the reader a step away from the row.
 *
 * **The two halves read different selectors on purpose.** A candidate must be
 * enabled, but a container is skipped for holding a stop of ANY kind: a button
 * row whose every button is disabled is still a container Steam's navigation
 * does not stop on, so focusing it would put the reader nowhere and take the
 * ring off the next real row. The test cannot tell that row from one carrying an
 * activate handler, so it skips both; a row whose only inner stop is disabled is
 * stepped over, which no body's first column produces today. Where that leaves
 * no candidate — no enabled stop that is free of stops inside it — nothing is
 * placed and the page keeps whatever Steam's retained pointer resolves to.
 */
export function firstBodyStop(root: ParentNode): HTMLElement | null {
  const stops = [...root.querySelectorAll<HTMLElement>(ENTRY_STOPS)];
  return stops.find((stop) => stop.querySelector(FOCUS_STOPS) === null) ?? null;
}

/**
 * The stop a narrow page opens on: its first enabled button.
 *
 * A narrow page is one column of Steam's own full-width rows, where the first
 * button IS the first row, so nothing is bought by walking the focus tree.
 */
export function firstPageButton(root: ParentNode): HTMLElement | null {
  return root.querySelector<HTMLElement>("button:not([disabled])");
}

/**
 * Long enough to land after Steam has finished mounting the page and resolving
 * its own focus pointer. The same 50 ms the scroll helpers use.
 */
export const ENTRY_FOCUS_DELAY_MS = 50;

/**
 * Put entry focus on the stop `findStop` picks out of `root`, and answer whether
 * there was one.
 *
 * `gpfocus` is the class Steam's own navigation adds to the focused element —
 * `.focus()` alone moves the DOM focus and leaves the element undrawn, so both
 * halves are needed for the reader to see where they are.
 */
export function placeEntryFocus(root: ParentNode, findStop: (root: ParentNode) => HTMLElement | null): boolean {
  const stop = findStop(root);
  if (!stop) return false;
  stop.focus();
  stop.classList.add("gpfocus");
  return true;
}
