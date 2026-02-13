import { definePlugin } from "@decky/api";
import { useState, FC } from "react";
import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { FaGamepad } from "react-icons/fa";
import { Settings } from "./components/Settings";
import { DownloadQueue } from "./components/DownloadQueue";

type Tab = "settings" | "downloads";

const QAMPanel: FC = () => {
  const [tab, setTab] = useState<Tab>("settings");

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => setTab("settings")}
            disabled={tab === "settings"}
          >
            Settings
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => setTab("downloads")}
            disabled={tab === "downloads"}
          >
            Downloads
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      {tab === "settings" ? <Settings /> : <DownloadQueue />}
    </>
  );
};

export default definePlugin(() => ({
  name: "RomM Library",
  icon: <FaGamepad />,
  content: <QAMPanel />,
  onDismount() {},
}));
