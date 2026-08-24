/**
 * PanelTabBar — the tab strip at the top of the RomM game detail panel.
 *
 * A tab exists only when its pane has something to show, which is why the panel
 * passes the three optional panes' presence rather than the state they are
 * derived from.
 *
 * CSS classes prefixed with `romm-tab` are injected separately by styleInjector.
 */

import { FC } from "react";
import { DialogButton, Focusable } from "@decky/ui";

interface PanelTabBarProps {
  activeTab: string;
  hasAchievements: boolean;
  hasSaves: boolean;
  hasBios: boolean;
  onSelect: (tabId: string) => void;
}

export const PanelTabBar: FC<PanelTabBarProps> = ({ activeTab, hasAchievements, hasSaves, hasBios, onSelect }) => {
  const tabs: { id: string; label: string; visible: boolean }[] = [
    { id: "info", label: "GAME INFO", visible: true },
    { id: "achievements", label: "ACHIEVEMENTS", visible: hasAchievements },
    { id: "saves", label: "SAVES", visible: hasSaves },
    { id: "bios", label: "BIOS", visible: hasBios },
  ];

  return (
    <Focusable className="romm-tab-bar" flow-children="right" data-romm="true">
      {tabs
        .filter((t) => t.visible)
        .map((t) => (
          <DialogButton
            key={`tab-${t.id}`}
            className={`romm-tab ${activeTab === t.id ? "romm-tab-active" : ""}`}
            onClick={() => onSelect(t.id)}
            style={{
              background: "transparent",
              border: "none",
              borderBottom: activeTab === t.id ? "2px solid #1a9fff" : "2px solid transparent",
              padding: "10px 16px",
              minWidth: "auto",
              width: "auto",
            }}
            noFocusRing={false}
          >
            {t.label}
          </DialogButton>
        ))}
    </Focusable>
  );
};
