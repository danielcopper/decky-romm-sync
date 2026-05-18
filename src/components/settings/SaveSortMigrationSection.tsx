/**
 * Conditional banner shown when RetroArch's save-sort flags have changed and
 * existing local saves need to be relocated. Pure renderer — the parent owns
 * the in-flight `migrating` flag and the result message; this section only
 * dispatches user intent (migrate / dismiss) back upstream.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, Field } from "@decky/ui";
import type { SaveSortMigrationStatus } from "../../api/backend";
import { sortLabel } from "./helpers";

interface SaveSortMigrationSectionProps {
  migration: SaveSortMigrationStatus;
  migrating: boolean;
  result: string;
  onMigrate: () => void;
  onDismiss: () => void;
}

export const SaveSortMigrationSection: FC<SaveSortMigrationSectionProps> = ({
  migration,
  migrating,
  result,
  onMigrate,
  onDismiss,
}) => {
  return (
    <PanelSection title="Save Sort Migration">
      <PanelSectionRow>
        <div style={{ padding: "8px 12px", backgroundColor: "rgba(212, 167, 44, 0.15)", borderLeft: "3px solid #d4a72c", borderRadius: "4px" }}>
          <div style={{ fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "6px" }}>
            {"⚠️"} RetroArch save sorting changed
          </div>
          {migration.old_settings && (
            <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "4px" }}>
              From: {sortLabel(migration.old_settings)}
            </div>
          )}
          {migration.new_settings && (
            <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "4px" }}>
              To: {sortLabel(migration.new_settings)}
            </div>
          )}
          <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.9)" }}>
            {migration.saves_count ?? 0} save file(s) to migrate
          </div>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={migrating}
          onClick={onMigrate}
        >
          {migrating ? "Migrating..." : "Migrate Save Files"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={migrating}
          onClick={onDismiss}
        >
          Dismiss (I migrated manually)
        </ButtonItem>
      </PanelSectionRow>
      {result && (
        <PanelSectionRow>
          <Field label={result} />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};
