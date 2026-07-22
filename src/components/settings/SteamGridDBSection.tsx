/**
 * SteamGridDB API key entry. Pure renderer: the parent owns the masked-key
 * display value and the verify/save handlers. The key is entered through
 * SgdbApiKeyModal, which validates it against SteamGridDB before saving, so this
 * section carries no status line of its own.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, DialogButton, Field, showModal } from "@decky/ui";
import { SgdbApiKeyModal } from "./SgdbApiKeyModal";
import type { VerifyKeyResult } from "./SgdbApiKeyModal";

interface SteamGridDBSectionProps {
  sgdbApiKey: string;
  onVerifyKey: (key: string) => Promise<VerifyKeyResult>;
  onSaveKey: (key: string) => Promise<void>;
}

export const SteamGridDBSection: FC<SteamGridDBSectionProps> = ({ sgdbApiKey, onVerifyKey, onSaveKey }) => {
  return (
    <PanelSection title="SteamGridDB">
      <PanelSectionRow>
        <Field label="API Key" description={sgdbApiKey ? "••••" : "Not configured"}>
          <DialogButton
            style={{ minWidth: "auto", width: "auto" }}
            onClick={() => showModal(<SgdbApiKeyModal onVerify={onVerifyKey} onSave={onSaveKey} />)}
          >
            Edit
          </DialogButton>
        </Field>
      </PanelSectionRow>
    </PanelSection>
  );
};
