/**
 * Plugin-wide developer/diagnostic toggles. Currently houses the log-level
 * dropdown; future additions belong here when they're orthogonal to any other
 * panel. Pure renderer: parent owns the current log level.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, DropdownItem } from "@decky/ui";

interface AdvancedSectionProps {
  logLevel: string;
  onLogLevelChange: (level: string) => void;
}

export const AdvancedSection: FC<AdvancedSectionProps> = ({ logLevel, onLogLevelChange }) => {
  return (
    <PanelSection title="Advanced">
      <PanelSectionRow>
        <DropdownItem
          label="Log Level"
          description="Controls how much detail is written to plugin logs"
          rgOptions={[
            { data: "error", label: "Error" },
            { data: "warn", label: "Warn" },
            { data: "info", label: "Info" },
            { data: "debug", label: "Debug" },
          ]}
          selectedOption={logLevel}
          onChange={(option) => onLogLevelChange(option.data)}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
