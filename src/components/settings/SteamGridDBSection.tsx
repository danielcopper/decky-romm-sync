/**
 * SteamGridDB API key + verify-key affordance. Pure renderer: parent owns
 * the masked-key display value, the verifying flag, and the status message.
 */

import { FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  DialogButton,
  showModal,
} from "@decky/ui";
import { TextInputModal } from "./TextInputModal";

interface SteamGridDBSectionProps {
  sgdbApiKey: string;
  sgdbStatus: string;
  sgdbVerifying: boolean;
  onSubmitKey: (value: string) => void;
  onVerifyKey: () => void;
}

export const SteamGridDBSection: FC<SteamGridDBSectionProps> = ({
  sgdbApiKey,
  sgdbStatus,
  sgdbVerifying,
  onSubmitKey,
  onVerifyKey,
}) => {
  return (
    <PanelSection title="SteamGridDB">
      <PanelSectionRow>
        <Field label="API Key" description={sgdbApiKey ? "••••" : "Not configured"}>
          <DialogButton onClick={() => showModal(
            <TextInputModal
              label="SteamGridDB API Key"
              value=""
              bIsPassword
              onSubmit={onSubmitKey}
            />
          )}>
            Edit
          </DialogButton>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={onVerifyKey}
          disabled={sgdbVerifying || !sgdbApiKey}
        >
          {sgdbVerifying ? "Verifying..." : "Verify Key"}
        </ButtonItem>
      </PanelSectionRow>
      {sgdbStatus && (
        <PanelSectionRow>
          <Field label={sgdbStatus} />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};
