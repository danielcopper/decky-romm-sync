import { FC } from "react";
import { PanelSection, PanelSectionRow, Field } from "@decky/ui";

export const DownloadQueue: FC = () => {
  return (
    <PanelSection title="Downloads">
      <PanelSectionRow>
        <Field label="No active downloads" />
      </PanelSectionRow>
    </PanelSection>
  );
};
