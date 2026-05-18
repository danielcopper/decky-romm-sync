/**
 * Save-sync toggles — master enable, sync-before-launch, sync-after-exit,
 * default save slot editor, history-limit dropdown, and the "Sync All" button.
 * Pure renderer: parent owns SaveSyncSettings, deviceInfo, and the sync status.
 */

import { FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  DropdownItem,
  DialogButton,
  showModal,
  ToggleField,
} from "@decky/ui";
import type { SaveSyncSettings as SaveSyncSettingsType } from "../../types";
import { TextInputModal } from "./TextInputModal";

interface SaveSyncSectionProps {
  saveSyncSettings: SaveSyncSettingsType | null;
  saveSyncToggleKey: number;
  deviceInfo: { device_id: string; device_name: string } | null;
  syncing: boolean;
  syncStatus: string;
  onToggleSaveSync: (value: boolean) => void;
  onSettingChange: (partial: Partial<SaveSyncSettingsType>) => void;
  onDefaultSlotSubmit: (value: string) => void;
  onResetDefaultSlot: () => void;
  onSyncAll: () => void;
}

export const SaveSyncSection: FC<SaveSyncSectionProps> = ({
  saveSyncSettings,
  saveSyncToggleKey,
  deviceInfo,
  syncing,
  syncStatus,
  onToggleSaveSync,
  onSettingChange,
  onDefaultSlotSubmit,
  onResetDefaultSlot,
  onSyncAll,
}) => {
  const saveSyncEnabled = saveSyncSettings?.save_sync_enabled ?? false;

  return (
    <PanelSection title="Save Sync">
      {saveSyncSettings ? (
        <>
          <PanelSectionRow>
            <ToggleField
              key={saveSyncToggleKey}
              label="Enable Save Sync"
              description="Sync RetroArch saves between this device and RomM server"
              checked={saveSyncEnabled}
              onChange={onToggleSaveSync}
            />
          </PanelSectionRow>
          {!saveSyncEnabled && (
            <PanelSectionRow>
              <Field label="Save sync is disabled" description="Enable above to configure sync settings" />
            </PanelSectionRow>
          )}
          {saveSyncEnabled && (
            <>
              {deviceInfo && (
                <PanelSectionRow>
                  <Field
                    label="Device"
                    description={`Registered as "${deviceInfo.device_name}"`}
                  />
                </PanelSectionRow>
              )}
              <PanelSectionRow>
                <ToggleField
                  label="Sync before launch"
                  description="Download newer saves from server before starting a game"
                  checked={saveSyncSettings.sync_before_launch}
                  onChange={(value) => onSettingChange({ sync_before_launch: value })}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <ToggleField
                  label="Sync after exit"
                  description="Upload changed saves to server after closing a game"
                  checked={saveSyncSettings.sync_after_exit}
                  onChange={(value) => onSettingChange({ sync_after_exit: value })}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <Field
                  label="Default Save Slot"
                  description={`${saveSyncSettings.default_slot || "(no slot)"} — applies to new games and games without a per-game slot override`}
                >
                  <DialogButton onClick={() => showModal(
                    <TextInputModal
                      label="Default Save Slot"
                      value={saveSyncSettings.default_slot ?? ""}
                      onSubmit={onDefaultSlotSubmit}
                    />
                  )}>
                    Edit
                  </DialogButton>
                </Field>
              </PanelSectionRow>
              {saveSyncSettings.default_slot !== "default" && (
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={onResetDefaultSlot}
                  >
                    Reset to default
                  </ButtonItem>
                </PanelSectionRow>
              )}
              {(saveSyncSettings.default_slot === null || saveSyncSettings.default_slot === "") && (
                <PanelSectionRow>
                  <Field
                    label={<span style={{ color: "#ff8800" }}>Legacy mode (no slot)</span>}
                    description="Saves are limited to one version per game."
                  />
                </PanelSectionRow>
              )}
              <PanelSectionRow>
                <DropdownItem
                  label="Save History Limit"
                  description="Max save versions kept per slot on the server"
                  rgOptions={[
                    { data: 5, label: "5" },
                    { data: 10, label: "10 (Default)" },
                    { data: 20, label: "20" },
                    { data: 50, label: "50" },
                  ]}
                  selectedOption={saveSyncSettings.autocleanup_limit ?? 10}
                  onChange={(option) => onSettingChange({ autocleanup_limit: option.data as number })}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={onSyncAll} disabled={syncing}>
                  {syncing ? "Syncing..." : "Sync All Saves Now"}
                </ButtonItem>
              </PanelSectionRow>
              {syncStatus && (
                <PanelSectionRow>
                  <Field label={syncStatus} />
                </PanelSectionRow>
              )}
            </>
          )}
        </>
      ) : (
        <PanelSectionRow>
          <Field label="Loading..." />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};
