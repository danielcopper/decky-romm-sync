import { findSP } from "./deckyUiInternals";

const ROMM_PLAY_HIDE_ID = "romm-hide-native-play";
const ROMM_FOCUS_STYLES_ID = "romm-focus-styles";
const ROMM_INFO_ITEMS_ID = "romm-info-items-styles";
const ROMM_GAME_INFO_PANEL_ID = "romm-game-info-panel-styles";
const ROMM_GEAR_BUTTONS_ID = "romm-gear-btn-styles";
const ROMM_TABS_ID = "romm-tabs-styles";
const ROMM_ACHIEVEMENTS_ID = "romm-achievements-styles";

export function hideNativePlaySection(playSectionClass: string) {
  const sp = findSP();
  if (!sp?.window.document) return;

  // Hide native PlaySection — display:none + visibility:hidden + pointer-events:none
  // All three are needed: display:none hides visually, visibility:hidden is belt-and-suspenders,
  // pointer-events:none is CRITICAL to remove from gamepad focus traversal
  if (!sp.window.document.getElementById(ROMM_PLAY_HIDE_ID)) {
    const style = sp.window.document.createElement("style");
    style.id = ROMM_PLAY_HIDE_ID;
    style.textContent = `.${playSectionClass}:not([data-romm]) {
  display: none !important;
  visibility: hidden !important;
  pointer-events: none !important;
}`;
    sp.window.document.head.appendChild(style);
  }

  // Inject .gpfocus styles for our custom buttons — Steam applies this class
  // automatically during gamepad navigation to indicate focus
  if (!sp.window.document.getElementById(ROMM_FOCUS_STYLES_ID)) {
    const focusStyle = sp.window.document.createElement("style");
    focusStyle.id = ROMM_FOCUS_STYLES_ID;
    focusStyle.textContent = `
.romm-btn-download:hover, .romm-btn-download.gpfocus {
  background: linear-gradient(to right, #47b3ff, #1a9fff) !important;
  filter: brightness(1.3);
}
.romm-btn-play:hover, .romm-btn-play.gpfocus {
  background: linear-gradient(to right, #80e62a, #01b866) !important;
  filter: brightness(1.2);
}
.romm-btn-play.romm-offline:hover, .romm-btn-play.romm-offline.gpfocus {
  background: linear-gradient(to right, #7a8b7a, #6b7b6b) !important;
  filter: brightness(1.1);
}
.romm-btn-conflict:hover, .romm-btn-conflict.gpfocus {
  background: linear-gradient(to right, #c49a28, #a6851b) !important;
  filter: brightness(1.2);
}
.romm-btn-dropdown:hover, .romm-btn-dropdown.gpfocus {
  filter: brightness(1.3);
}
.romm-btn-cancel:hover, .romm-btn-cancel.gpfocus, .romm-btn-cancel:focus {
  background: rgba(255, 255, 255, 0.25) !important;
}
[data-romm] .gpfocus {
  outline: 2px solid #1a9fff;
  outline-offset: 2px;
}
.romm-wizard-btn:hover,
.romm-wizard-btn:focus,
.romm-wizard-btn.gpfocus {
  background: rgba(255, 255, 255, 0.1) !important;
  border-color: rgba(255, 255, 255, 0.7) !important;
  outline: 2px solid #1a9fff !important;
  outline-offset: 2px;
}
.romm-wizard-btn-primary:hover,
.romm-wizard-btn-primary:focus,
.romm-wizard-btn-primary.gpfocus {
  background: rgba(26, 159, 255, 0.3) !important;
  border-color: rgba(26, 159, 255, 0.9) !important;
  outline: 2px solid #1a9fff !important;
  outline-offset: 2px;
}
@keyframes romm-spin {
  to { transform: rotate(360deg); }
}
.romm-throbber {
  display: inline-block;
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: romm-spin 0.8s linear infinite;
  flex-shrink: 0;
}
@keyframes romm-dl-pulse {
  0%, 100% { box-shadow: 0 0 4px var(--romm-pulse-color, rgba(26,159,255,0.3)); }
  50% { box-shadow: 0 0 20px var(--romm-pulse-color, rgba(26,159,255,0.7)); }
}
@keyframes romm-dl-complete {
  0% { box-shadow: 0 0 0px rgba(112,214,29,0); filter: brightness(1); transform: scale(1); }
  12% { box-shadow: 0 0 50px rgba(112,214,29,1), inset 0 0 25px rgba(255,255,255,0.4); filter: brightness(2.2); transform: scale(1.06); }
  30% { box-shadow: 0 0 30px rgba(112,214,29,0.7), inset 0 0 12px rgba(255,255,255,0.2); filter: brightness(1.5); transform: scale(1.02); }
  60% { box-shadow: 0 0 15px rgba(112,214,29,0.4), inset 0 0 5px rgba(255,255,255,0.08); filter: brightness(1.2); transform: scale(1); }
  100% { box-shadow: 0 0 0px rgba(112,214,29,0); filter: brightness(1.2); transform: scale(1); }
}
.romm-btn-download {
  position: relative;
  overflow: hidden;
}
.romm-dl-fill {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  width: 0%;
  transition: width 0.4s ease-out;
  pointer-events: none;
  z-index: 0;
}
.romm-dl-active-group {
  border-radius: 2px;
  animation: romm-dl-pulse 2s ease-in-out infinite;
}
.romm-dl-label {
  position: relative;
  z-index: 2;
}
.romm-dl-complete-flash {
  animation: romm-dl-complete 1s ease-out forwards;
}
@keyframes romm-dl-uninstall {
  0% { box-shadow: 0 0 0px rgba(26,159,255,0); filter: brightness(0.4); transform: scale(0.95); }
  40% { box-shadow: 0 0 35px rgba(26,159,255,0.8); filter: brightness(1.5); transform: scale(1.03); }
  100% { box-shadow: 0 0 0px rgba(26,159,255,0); filter: brightness(1.3); transform: scale(1); }
}
.romm-dl-uninstall-flash {
  animation: romm-dl-uninstall 0.5s ease-out forwards;
}`;
    sp.window.document.head.appendChild(focusStyle);
  }

  // Info items styles for RomMPlaySection (Last Played, Playtime, Save Sync, BIOS)
  if (!sp.window.document.getElementById(ROMM_INFO_ITEMS_ID)) {
    const infoStyle = sp.window.document.createElement("style");
    infoStyle.id = ROMM_INFO_ITEMS_ID;
    infoStyle.textContent = `
.romm-info-items {
  margin-left: 16px;
}
.romm-info-item {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
}
.romm-info-header {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: #8f98a0;
  line-height: 1.2;
  white-space: nowrap;
}
.romm-info-value {
  font-size: 13px;
  font-weight: 500;
  color: #dcdedf;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.romm-info-muted .romm-info-value {
  color: #5e6770;
  font-style: italic;
}
.romm-status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
@keyframes romm-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
.romm-status-dot-pulse {
  animation: romm-pulse 1.5s ease-in-out infinite;
}`;
    sp.window.document.head.appendChild(infoStyle);
  }

  // Game info panel styles for RomMGameInfoPanel (Description, Metadata, Genres, ROM File)
  if (!sp.window.document.getElementById(ROMM_GAME_INFO_PANEL_ID)) {
    const panelStyle = sp.window.document.createElement("style");
    panelStyle.id = ROMM_GAME_INFO_PANEL_ID;
    panelStyle.textContent = `
.romm-panel-container {
  display: flex;
  flex-direction: column;
  padding: 16px 2.8vw;
  gap: 16px;
  background: rgba(14, 20, 27, 0.33);
}
.romm-panel-section {
  padding-bottom: 16px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
}
.romm-panel-section:last-child {
  border-bottom: none;
  padding-bottom: 0;
}
.romm-panel-section-title {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: #8f98a0;
  margin-bottom: 8px;
  line-height: 1.2;
}
.romm-panel-rom-name {
  color: #ffffff;
  font-size: 16px;
  font-weight: 700;
  line-height: 1.25;
  margin-bottom: 8px;
}
.romm-panel-summary {
  color: #acb2b8;
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: 8px;
}
.romm-panel-info-row {
  display: flex;
  align-items: baseline;
  gap: 16px;
  padding: 2px 0;
}
.romm-panel-label {
  color: #8f98a0;
  font-size: 12px;
  flex-shrink: 0;
  min-width: 120px;
}
.romm-panel-value {
  color: #dcdedf;
  font-size: 13px;
}
.romm-panel-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.romm-panel-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 3px;
  background: rgba(255,255,255,0.06);
  color: #8f98a0;
  border: 1px solid rgba(255,255,255,0.1);
}
.romm-panel-status-row {
  display: flex;
  gap: 12px;
  align-items: center;
}
.romm-panel-status-badge {
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 3px;
  font-weight: 600;
}
.romm-panel-status-installed {
  background: rgba(91,163,43,0.2);
  color: #5ba32b;
}
.romm-panel-status-not-installed {
  background: rgba(143,152,160,0.15);
  color: #8f98a0;
}
.romm-panel-platform-badge {
  font-size: 11px;
  padding: 3px 10px;
  border-radius: 3px;
  font-weight: 600;
  background: rgba(26,159,255,0.15);
  color: #1a9fff;
}
.romm-panel-actions-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.romm-panel-action-btn {
  font-size: 12px;
  padding: 6px 14px;
  border-radius: 3px;
  background: rgba(255,255,255,0.06);
  color: #dcdedf;
  border: 1px solid rgba(255,255,255,0.1);
  cursor: pointer;
}
.romm-panel-action-btn:hover, .romm-panel-action-btn.gpfocus {
  background: rgba(255,255,255,0.12);
  filter: brightness(1.1);
}
.romm-panel-action-destructive {
  color: #d94126;
  border-color: rgba(217,65,38,0.3);
}
.romm-panel-action-destructive:hover, .romm-panel-action-destructive.gpfocus {
  background: rgba(217,65,38,0.15);
}
.romm-panel-muted {
  color: #5e6770;
  font-size: 13px;
  font-style: italic;
}
.romm-panel-loading {
  color: #8f98a0;
  font-size: 13px;
  padding: 8px 0;
}
.romm-panel-status-action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.romm-panel-status-inline {
  display: flex;
  align-items: center;
  gap: 8px;
}
.romm-panel-file-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0;
}
.romm-panel-file-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}
.romm-panel-file-name {
  font-size: 12px;
  color: #dcdedf;
  font-weight: 500;
}
.romm-panel-file-path {
  font-size: 11px;
  color: #5e6770;
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.romm-panel-file-detail {
  font-size: 11px;
  color: #8f98a0;
  white-space: nowrap;
}
.romm-panel-file-conflict {
  font-size: 11px;
  color: #d94126;
  font-weight: 600;
}
/* Slot panel (collapsible) */
.romm-slot-panel {
  margin-bottom: 8px;
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px;
  overflow: hidden;
}
.romm-slot-panel-active {
  border-color: rgba(91,163,43,0.3);
}
.romm-slot-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  cursor: pointer;
  background: rgba(255,255,255,0.03);
  transition: background 0.15s;
}
.romm-slot-header:hover {
  background: rgba(255,255,255,0.06);
}
.romm-slot-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.romm-slot-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.romm-slot-name {
  font-size: 13px;
  font-weight: 600;
  color: #dcdedf;
}
.romm-slot-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
  text-transform: uppercase;
}
.romm-slot-badge-active {
  background: rgba(91,163,43,0.2);
  color: #5ba32b;
}
.romm-slot-badge-server {
  background: rgba(26,159,255,0.15);
  color: #1a9fff;
}
.romm-slot-badge-local {
  background: rgba(212,167,44,0.15);
  color: #d4a72c;
}
.romm-slot-count {
  font-size: 12px;
  color: #8f98a0;
}
.romm-slot-chevron {
  font-size: 14px;
  color: #8f98a0;
  transition: transform 0.2s;
}
.romm-slot-body {
  padding: 8px 12px 12px;
  border-top: 1px solid rgba(255,255,255,0.06);
}
.romm-slot-sync-summary {
  font-size: 12px;
  padding: 4px 12px 0;
}
.romm-save-status-label {
  font-size: 11px;
  font-weight: 600;
}`;
    sp.window.document.head.appendChild(panelStyle);
  }

  // Gear icon button styles for RomMPlaySection (RomM actions + Steam properties)
  if (!sp.window.document.getElementById(ROMM_GEAR_BUTTONS_ID)) {
    const gearStyle = sp.window.document.createElement("style");
    gearStyle.id = ROMM_GEAR_BUTTONS_ID;
    gearStyle.textContent = `
.romm-gear-btn {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: 36px !important;
  min-width: 36px !important;
  height: 36px !important;
  border-radius: 4px !important;
  border: 1px solid rgba(255,255,255,0.1);
  background: rgba(255,255,255,0.06) !important;
  cursor: pointer;
  transition: background 0.15s ease, filter 0.15s ease;
  padding: 0 !important;
  flex-shrink: 0;
  line-height: normal !important;
}
.romm-gear-btn:hover {
  background: rgba(255,255,255,0.14);
  filter: brightness(1.2);
}
.romm-gear-btn.gpfocus {
  background: rgba(255,255,255,0.14) !important;
  filter: brightness(1.3);
  border-color: rgba(255,255,255,0.5) !important;
}
.romm-gear-btn:active {
  filter: brightness(0.9);
}`;
    sp.window.document.head.appendChild(gearStyle);
  }

  // Tab bar styles for game detail page tabs (GAME INFO | ACHIEVEMENTS | SAVES | BIOS)
  if (!sp.window.document.getElementById(ROMM_TABS_ID)) {
    const tabStyle = sp.window.document.createElement("style");
    tabStyle.id = ROMM_TABS_ID;
    tabStyle.textContent = `
.romm-tab-bar {
  display: flex;
  gap: 0;
  border-bottom: 1px solid rgba(255,255,255,0.08);
  padding: 0 2.8vw;
  background: rgba(14, 20, 27, 0.33);
}
.romm-tab {
  padding: 10px 16px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: #8f98a0;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.15s ease, border-color 0.15s ease;
  background: transparent;
  border-top: none;
  border-left: none;
  border-right: none;
  white-space: nowrap;
}
.romm-tab:hover {
  color: #dcdedf !important;
}
.romm-tab.gpfocus,
.romm-tab:focus,
.romm-tab.Focusable.gpfocus {
  color: #dcdedf !important;
  background: rgba(255,255,255,0.06) !important;
  outline: none;
  border-bottom-color: #1a9fff;
}
.romm-tab-active {
  color: #dcdedf !important;
  border-bottom-color: #1a9fff;
}
.romm-tab-content {
  padding: 16px 2.8vw;
  background: rgba(14, 20, 27, 0.33);
}`;
    sp.window.document.head.appendChild(tabStyle);
  }

  // Achievement badge sparkle + achievements tab styles
  if (!sp.window.document.getElementById(ROMM_ACHIEVEMENTS_ID)) {
    const cheevoStyle = sp.window.document.createElement("style");
    cheevoStyle.id = ROMM_ACHIEVEMENTS_ID;
    cheevoStyle.textContent = `
@keyframes romm-gold-sparkle {
  0%, 100% { opacity: 0; transform: scale(0); }
  15% { opacity: 1; transform: scale(1.2); }
  30% { opacity: 0.9; transform: scale(0.9); }
  50% { opacity: 1; transform: scale(1); }
  80% { opacity: 0.4; transform: scale(0.6); }
}
.romm-cheevo-badge {
  position: relative;
  cursor: pointer;
}
.romm-cheevo-badge-sparkle {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.romm-sparkle-container {
  position: absolute;
  top: -4px;
  left: -4px;
  right: -4px;
  bottom: -4px;
  pointer-events: none;
  overflow: visible;
}
.romm-sparkle-dot {
  position: absolute;
  width: 2px;
  height: 2px;
  border-radius: 50%;
  background: #fff8dc;
  box-shadow: 0 0 2px 1px #ffd700, 0 0 4px 1px rgba(255, 215, 0, 0.5);
  animation: romm-gold-sparkle var(--romm-sparkle-dur, 2s) ease-in-out infinite;
  animation-delay: var(--romm-sparkle-delay, 0s);
  top: var(--romm-sparkle-top, 50%);
  left: var(--romm-sparkle-left, 50%);
}
.romm-cheevo-trophy {
  color: #ffd700;
  font-size: 14px;
  filter: drop-shadow(0 0 2px rgba(255, 215, 0, 0.5));
}
.romm-cheevo-trophy-none {
  color: #8f98a0;
  font-size: 14px;
  filter: none;
}
.romm-cheevo-count {
  font-size: 13px;
  font-weight: 500;
  color: #dcdedf;
}
.romm-cheevo-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.romm-cheevo-progress-bar {
  height: 6px;
  background: rgba(255,255,255,0.08);
  border-radius: 3px;
  overflow: hidden;
  margin: 8px 0 16px 0;
}
.romm-cheevo-progress-fill {
  height: 100%;
  background: linear-gradient(to right, #ffd700, #ffaa00);
  border-radius: 3px;
  transition: width 0.4s ease-out;
}
.romm-cheevo-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px;
  border-radius: 4px;
  background: rgba(255,255,255,0.03);
  border-left: 3px solid transparent;
}
.romm-cheevo-row-earned {
  background: rgba(255, 215, 0, 0.06);
  border-left-color: #ffd700;
}
.romm-cheevo-img-wrap {
  position: relative;
  flex-shrink: 0;
  width: 48px;
  height: 48px;
}
.romm-cheevo-img-sparkles {
  position: absolute;
  top: -5px;
  left: -5px;
  right: -5px;
  bottom: -5px;
  pointer-events: none;
  overflow: visible;
}
.romm-cheevo-img-sparkle-dot {
  position: absolute;
  width: 2.5px;
  height: 2.5px;
  border-radius: 50%;
  background: #fff8dc;
  box-shadow: 0 0 2px 1px #ffd700, 0 0 5px 1px rgba(255, 215, 0, 0.5);
  animation: romm-gold-sparkle var(--romm-sparkle-dur, 2.5s) ease-in-out infinite;
  animation-delay: var(--romm-sparkle-delay, 0s);
  top: var(--romm-sparkle-top, 50%);
  left: var(--romm-sparkle-left, 50%);
}
.romm-cheevo-badge-img {
  width: 48px;
  height: 48px;
  border-radius: 4px;
  flex-shrink: 0;
  object-fit: cover;
  background: rgba(255,255,255,0.05);
}
.romm-cheevo-details {
  flex: 1;
  min-width: 0;
}
.romm-cheevo-title {
  font-size: 13px;
  font-weight: 600;
  color: #dcdedf;
  line-height: 1.3;
}
.romm-cheevo-desc {
  font-size: 12px;
  color: #8f98a0;
  line-height: 1.4;
  margin-top: 2px;
}
.romm-cheevo-points {
  font-size: 11px;
  color: #ffd700;
  font-weight: 600;
  flex-shrink: 0;
  padding: 2px 8px;
  background: rgba(255, 215, 0, 0.1);
  border-radius: 3px;
}
.romm-cheevo-points-locked {
  color: #8f98a0;
  background: rgba(255,255,255,0.05);
}
.romm-cheevo-earned-label {
  font-size: 10px;
  color: #5ba32b;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.romm-cheevo-section-title {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: #8f98a0;
  margin: 12px 0 8px 0;
}
.romm-cheevo-summary {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 8px;
}
.romm-cheevo-summary-text {
  font-size: 14px;
  font-weight: 600;
  color: #dcdedf;
}
.romm-cheevo-summary-sub {
  font-size: 12px;
  color: #8f98a0;
}
.romm-cheevo-rarity {
  font-size: 10px;
  color: #8f98a0;
  white-space: nowrap;
}
.romm-cheevo-dates {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}
.romm-cheevo-date {
  font-size: 10px;
  color: #8f98a0;
  white-space: nowrap;
  padding: 2px 6px;
  background: rgba(255,255,255,0.06);
  border-radius: 3px;
}
.romm-cheevo-hc-badge {
  display: inline-flex;
  align-items: center;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #ffd700;
  padding: 1px 5px;
  background: rgba(255, 215, 0, 0.15);
  border-radius: 3px;
  box-shadow: 0 0 4px rgba(255, 215, 0, 0.3), 0 0 8px rgba(255, 215, 0, 0.15);
  text-shadow: 0 0 4px rgba(255, 215, 0, 0.5);
}
.romm-cheevo-badge-img-hc {
  box-shadow: 0 0 6px rgba(255, 215, 0, 0.4), 0 0 12px rgba(255, 215, 0, 0.2);
  border: 1px solid rgba(255, 215, 0, 0.3);
}`;
    sp.window.document.head.appendChild(cheevoStyle);
  }
}

export function showNativePlaySection() {
  const sp = findSP();
  if (!sp?.window.document) return;
  sp.window.document.getElementById(ROMM_PLAY_HIDE_ID)?.remove();
  sp.window.document.getElementById(ROMM_FOCUS_STYLES_ID)?.remove();
  sp.window.document.getElementById(ROMM_INFO_ITEMS_ID)?.remove();
  sp.window.document.getElementById(ROMM_GAME_INFO_PANEL_ID)?.remove();
  sp.window.document.getElementById(ROMM_GEAR_BUTTONS_ID)?.remove();
  sp.window.document.getElementById(ROMM_TABS_ID)?.remove();
  sp.window.document.getElementById(ROMM_ACHIEVEMENTS_ID)?.remove();
}
