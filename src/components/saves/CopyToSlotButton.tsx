/**
 * Shared "Copy to slot…" button rendered on every save row that carries a
 * non-null server save id — the legacy bucket + inactive slots (ServerSaveRow),
 * the active tracked file (SaveFileRow), and version-history rows. Pure render
 * helper: the click hands (saveId, sourceSlot) to the parent's `onCopy`, which
 * owns the picker modal and the copy flow (`useCopyToSlot`). Disabled offline.
 */

import type { ReactElement } from "react";
import { DialogButton } from "@decky/ui";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";

/** Callback opening the copy-to-slot picker for one source save. */
export type CopyToSlotHandler = (saveId: number, sourceSlot: string) => void;

/** Bundle threaded down to each row that can offer the copy action. */
export interface CopyToSlotRowProps {
  onCopy: CopyToSlotHandler;
  sourceSlot: string;
  isOffline: boolean;
}

export function renderCopyToSlotButton(key: string, saveId: number, copy: CopyToSlotRowProps): ReactElement {
  return (
    <DialogButton
      key={key}
      style={{
        padding: "2px 8px",
        minWidth: "auto",
        fontSize: "11px",
        width: "auto",
        flexShrink: 0,
      }}
      noFocusRing={false}
      onFocus={scrollFocusedToCenter}
      disabled={copy.isOffline}
      onClick={() => {
        copy.onCopy(saveId, copy.sourceSlot);
      }}
    >
      Copy to slot…
    </DialogButton>
  );
}
