import { FC, useEffect, useState } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import { toaster } from "@decky/api";
import {
  migrateRetroDeckFiles,
  dismissRetrodeckMigration,
} from "../api/backend";
import type { MigrationStatus } from "../types";
import {
  getMigrationState,
  onMigrationChange,
  clearMigration,
} from "../utils/migrationStore";
import { MigrationConflictModal } from "./MigrationConflictModal";
import { scrollToTop } from "../utils/scrollHelpers";

/**
 * Subscribe to migration state changes.
 * Returns the current migration status.
 */
export function useMigrationStatus(): MigrationStatus {
  const [status, setStatus] = useState<MigrationStatus>(getMigrationState());
  useEffect(() => onMigrationChange(() => setStatus(getMigrationState())), []);
  return status;
}

interface MigrationBlockedPageProps {
  migration: MigrationStatus;
}

export const MigrationBlockedPage: FC<MigrationBlockedPageProps> = ({ migration }) => {
  const [migrating, setMigrating] = useState(false);
  const [migrateResult, setMigrateResult] = useState("");

  const runMigration = async (strategy: "overwrite" | "skip" | null) => {
    setMigrating(true);
    setMigrateResult("");
    try {
      const result = await migrateRetroDeckFiles(strategy);
      if (result.needs_confirmation) {
        setMigrating(false);
        showModal(
          <MigrationConflictModal
            conflictCount={result.conflict_count ?? 0}
            onChoice={(s) => { runMigration(s); }}
          />,
        );
        return;
      }
      setMigrateResult(result.message);
      if (result.success) {
        clearMigration();
        toaster.toast({
          title: "RomM Sync",
          body: result.message || "Migration complete.",
        });
      }
    } catch {
      setMigrateResult("Migration failed");
    }
    setMigrating(false);
  };

  const handleMigrate = () => { runMigration(null); };

  const handleDismiss = () => {
    showModal(
      <ConfirmModal
        strTitle="Dismiss Migration?"
        strDescription={
          "This will accept that some ROMs and saves remain at the old location. " +
          "The plugin will continue with the new path. Save data may be inconsistent " +
          "across sessions. Are you sure?"
        }
        strOKButtonText="Dismiss"
        strCancelButtonText="Cancel"
        onOK={async () => {
          try {
            const result = await dismissRetrodeckMigration();
            if (result.success) {
              clearMigration();
              toaster.toast({
                title: "RomM Sync",
                body: "Migration dismissed.",
              });
            }
          } catch {
            setMigrateResult("Dismiss failed");
          }
        }}
      />,
    );
  };

  return (
    <PanelSection title="RetroDECK Migration Required">
      <PanelSectionRow>
        <div
          style={{
            padding: "8px 12px",
            backgroundColor: "rgba(212, 167, 44, 0.15)",
            borderLeft: "3px solid #d4a72c",
            borderRadius: "4px",
          }}
        >
          <div
            style={{
              fontSize: "13px",
              fontWeight: "bold",
              color: "#d4a72c",
              marginBottom: "6px",
            }}
          >
            {"⚠️"} RetroDECK location changed
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "4px" }}>
            From: {migration.old_path ?? "unknown"}
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "4px" }}>
            To: {migration.new_path ?? "unknown"}
          </div>
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.9)" }}>
            {migration.roms_count ?? 0} ROM(s), {migration.bios_count ?? 0} BIOS,{" "}
            {migration.saves_count ?? 0} save(s) to migrate
          </div>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={migrating}
          onClick={handleMigrate}
          // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
          onFocus={scrollToTop}
        >
          {migrating ? "Migrating..." : "Migrate Files"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={migrating} onClick={handleDismiss}>
          Dismiss
        </ButtonItem>
      </PanelSectionRow>
      {migrateResult && (
        <PanelSectionRow>
          <Field label={migrateResult} />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <div style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.55)", padding: "4px 0" }}>
          Or revert RetroDECK to its previous location — the plugin will detect it automatically.
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
};
