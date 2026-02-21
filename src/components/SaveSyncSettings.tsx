import { useState, useEffect, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  DropdownItem,
  ToggleField,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import {
  getSaveSyncSettings,
  updateSaveSyncSettings,
  syncAllSaves,
  getPendingConflicts,
  getOfflineQueue,
  retryFailedSync,
  clearOfflineQueue,
} from "../api/backend";
import { showConflictResolutionModal } from "./ConflictModal";
import type { SaveSyncSettings as SaveSyncSettingsType, PendingConflict, ConflictMode, OfflineQueueItem } from "../types";

interface SaveSyncSettingsProps {
  onBack: () => void;
}

const conflictModeOptions = [
  { data: "newest_wins" as ConflictMode, label: "Newest Wins (Default)" },
  { data: "always_upload" as ConflictMode, label: "Always Upload" },
  { data: "always_download" as ConflictMode, label: "Always Download" },
  { data: "ask_me" as ConflictMode, label: "Ask Me" },
];

export const SaveSyncSettings: FC<SaveSyncSettingsProps> = ({ onBack }) => {
  const [settings, setSettings] = useState<SaveSyncSettingsType | null>(null);
  const [conflicts, setConflicts] = useState<PendingConflict[]>([]);
  const [failedOps, setFailedOps] = useState<OfflineQueueItem[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [syncStatus, setSyncStatus] = useState("");
  const [resolving, setResolving] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);

  useEffect(() => {
    getSaveSyncSettings()
      .then(setSettings)
      .catch((e) => console.error("[RomM] Failed to load save sync settings:", e));
    loadConflicts();
    loadFailedOps();
  }, []);

  const loadConflicts = async () => {
    try {
      const result = await getPendingConflicts();
      setConflicts(result.conflicts);
    } catch (e) {
      console.error("[RomM] Failed to load conflicts:", e);
    }
  };

  const loadFailedOps = async () => {
    try {
      const result = await getOfflineQueue();
      setFailedOps(result.queue);
    } catch (e) {
      console.error("[RomM] Failed to load offline queue:", e);
    }
  };

  const handleSettingChange = async (partial: Partial<SaveSyncSettingsType>) => {
    if (!settings) return;
    const updated = { ...settings, ...partial };
    setSettings(updated);
    try {
      await updateSaveSyncSettings(updated);
    } catch (e) {
      console.error("[RomM] Failed to save settings:", e);
    }
  };

  const handleSyncAll = async () => {
    setSyncing(true);
    setSyncStatus("");
    try {
      const result = await syncAllSaves();
      setSyncStatus(result.message);
      if (result.conflicts > 0) {
        await loadConflicts();
      }
      await loadFailedOps();
    } catch {
      setSyncStatus("Sync failed");
    }
    setSyncing(false);
  };

  const handleResolveConflict = async (conflict: PendingConflict) => {
    const key = `${conflict.rom_id}:${conflict.filename}`;
    setResolving(key);

    const resolution = await showConflictResolutionModal([conflict]);
    if (resolution === "use_local" || resolution === "use_server") {
      // Conflict was resolved by the modal — remove from local list
      setConflicts((prev) =>
        prev.filter((c) => !(c.rom_id === conflict.rom_id && c.filename === conflict.filename)),
      );
    }
    // "skip", "launch_anyway", "cancel" leave the conflict in the list

    setResolving(null);
  };

  const handleRetry = async (item: OfflineQueueItem) => {
    const key = `${item.rom_id}:${item.filename}`;
    setRetrying(key);
    try {
      const result = await retryFailedSync(item.rom_id, item.filename);
      if (result.success) {
        setFailedOps((prev) =>
          prev.filter((f) => !(f.rom_id === item.rom_id && f.filename === item.filename)),
        );
      }
    } catch (e) {
      console.error("[RomM] Retry failed:", e);
    }
    setRetrying(null);
  };

  const handleClearQueue = async () => {
    try {
      await clearOfflineQueue();
      setFailedOps([]);
    } catch (e) {
      console.error("[RomM] Failed to clear queue:", e);
    }
  };

  if (!settings) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>Back</ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Loading..." />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const enabled = settings.save_sync_enabled;

  const handleToggleEnable = (value: boolean) => {
    if (value) {
      // Two-step confirmation before enabling
      showModal(
        <ConfirmModal
          strTitle="Enable Save Sync?"
          strDescription="This will sync RetroArch save files between this device and your RomM server. Make sure you are NOT using a shared RomM account (e.g. admin, romm, guest) — save sync is per-user."
          strOKButtonText="Enable"
          strCancelButtonText="Cancel"
          onOK={() => handleSettingChange({ save_sync_enabled: true })}
        />,
      );
    } else {
      handleSettingChange({ save_sync_enabled: false });
    }
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Save Sync">
        <PanelSectionRow>
          <ToggleField
            label="Enable Save Sync"
            description="Sync RetroArch saves between this device and RomM server"
            checked={enabled}
            onChange={handleToggleEnable}
          />
        </PanelSectionRow>
        {!enabled && (
          <PanelSectionRow>
            <Field label="Save sync is disabled" description="Enable above to configure sync settings" />
          </PanelSectionRow>
        )}
      </PanelSection>

      {enabled && (
        <>
          <PanelSection title="Auto Sync">
            <PanelSectionRow>
              <ToggleField
                label="Sync before launch"
                description="Download newer saves from server before starting a game"
                checked={settings.sync_before_launch}
                onChange={(value) => handleSettingChange({ sync_before_launch: value })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Sync after exit"
                description="Upload changed saves to server after closing a game"
                checked={settings.sync_after_exit}
                onChange={(value) => handleSettingChange({ sync_after_exit: value })}
              />
            </PanelSectionRow>
          </PanelSection>

          <PanelSection title="Conflict Resolution">
            <PanelSectionRow>
              <DropdownItem
                label="When saves conflict"
                description="How to handle conflicting save files between devices"
                rgOptions={conflictModeOptions}
                selectedOption={settings.conflict_mode}
                onChange={(option) => handleSettingChange({ conflict_mode: option.data as ConflictMode })}
              />
            </PanelSectionRow>
          </PanelSection>

          <PanelSection title="Manual Sync">
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={handleSyncAll} disabled={syncing}>
                {syncing ? "Syncing..." : "Sync All Saves Now"}
              </ButtonItem>
            </PanelSectionRow>
            {syncStatus && (
              <PanelSectionRow>
                <Field label={syncStatus} />
              </PanelSectionRow>
            )}
          </PanelSection>

          {failedOps.length > 0 && (
            <PanelSection title={`Failed Syncs (${failedOps.length})`}>
              {failedOps.map((item) => {
                const key = `${item.rom_id}:${item.filename}`;
                const isRetrying = retrying === key;
                return (
                  <PanelSectionRow key={key}>
                    <Field
                      label={item.filename}
                      description={`ROM #${item.rom_id} \u2014 ${item.error} \u2014 ${formatTimeAgo(item.failed_at)}`}
                    >
                      <ButtonItem
                        layout="below"
                        onClick={() => handleRetry(item)}
                        disabled={isRetrying}
                      >
                        {isRetrying ? "Retrying..." : "Retry Now"}
                      </ButtonItem>
                    </Field>
                  </PanelSectionRow>
                );
              })}
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={handleClearQueue}>
                  Clear All Failed
                </ButtonItem>
              </PanelSectionRow>
            </PanelSection>
          )}

          {conflicts.length > 0 && (
            <PanelSection title={`Conflicts (${conflicts.length})`}>
              {conflicts.map((c) => {
                const key = `${c.rom_id}:${c.filename}`;
                const isResolving = resolving === key;
                return (
                  <PanelSectionRow key={key}>
                    <Field
                      label={c.filename}
                      description={`ROM #${c.rom_id} \u2014 detected ${formatTimeAgo(c.created_at)}`}
                    >
                      <ButtonItem
                        layout="below"
                        onClick={() => handleResolveConflict(c)}
                        disabled={isResolving}
                      >
                        {isResolving ? "Resolving..." : "Resolve"}
                      </ButtonItem>
                    </Field>
                  </PanelSectionRow>
                );
              })}
            </PanelSection>
          )}
        </>
      )}
    </>
  );
};

function formatTimeAgo(iso: string): string {
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return "just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
  } catch {
    return iso;
  }
}
