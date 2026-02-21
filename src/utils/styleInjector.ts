import { findSP } from "@decky/ui";

const ROMM_PLAY_HIDE_ID = "romm-hide-native-play";

export function hideNativePlaySection(playSectionClass: string) {
  const sp = findSP();
  if (!sp?.window?.document) return;
  if (sp.window.document.getElementById(ROMM_PLAY_HIDE_ID)) return;
  const style = sp.window.document.createElement("style");
  style.id = ROMM_PLAY_HIDE_ID;
  style.textContent = `.${playSectionClass}:not([data-romm]) { display: none !important; }`;
  sp.window.document.head.appendChild(style);
}

export function showNativePlaySection() {
  const sp = findSP();
  if (!sp?.window?.document) return;
  sp.window.document.getElementById(ROMM_PLAY_HIDE_ID)?.remove();
}
