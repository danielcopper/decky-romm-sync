import { useState, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  showModal,
  ConfirmModal,
} from "@decky/ui";
import { removeAllShortcuts } from "../api/backend";

interface DangerZoneProps {
  onBack: () => void;
}

export const DangerZone: FC<DangerZoneProps> = ({ onBack }) => {
  const [status, setStatus] = useState("");

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Danger Zone">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              showModal(
                <ConfirmModal
                  strTitle="Remove All Shortcuts"
                  strDescription="Remove all RomM games from your Steam Library? Downloaded ROMs will not be deleted."
                  strOKButtonText="Remove All"
                  onOK={async () => {
                    const result = await removeAllShortcuts();
                    setStatus(result.message);
                  }}
                />
              );
            }}
          >
            Remove All RomM Shortcuts
          </ButtonItem>
        </PanelSectionRow>
        {status && (
          <PanelSectionRow>
            <Field label={status} />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};
