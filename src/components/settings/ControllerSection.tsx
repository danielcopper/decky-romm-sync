/**
 * Controller settings — Steam Input mode dropdown, "Apply to All Shortcuts"
 * trigger, and the RetroArch input_driver warning + auto-fix affordance.
 * Pure renderer: parent owns the mode value, status strings, and warning data.
 */

import { FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  DropdownItem,
} from "@decky/ui";
import type { RetroArchInputCheck } from "../../types";

interface ControllerSectionProps {
  steamInputMode: string;
  steamInputStatus: string;
  retroarchWarning: RetroArchInputCheck | null;
  retroarchFixStatus: string;
  loading: boolean;
  onModeChange: (mode: string) => void;
  onApplyMode: () => void;
  onFixInputDriver: () => void;
}

export const ControllerSection: FC<ControllerSectionProps> = ({
  steamInputMode,
  steamInputStatus,
  retroarchWarning,
  retroarchFixStatus,
  loading,
  onModeChange,
  onApplyMode,
  onFixInputDriver,
}) => {
  return (
    <PanelSection title="Controller">
      <PanelSectionRow>
        <DropdownItem
          label="Steam Input Mode"
          description="Controls how Steam handles controller input for ROM shortcuts"
          rgOptions={[
            { data: "default", label: "Default (Recommended)" },
            { data: "force_on", label: "Force On" },
            { data: "force_off", label: "Force Off" },
          ]}
          selectedOption={steamInputMode}
          onChange={(option) => onModeChange(option.data)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={onApplyMode}
          disabled={loading}
        >
          Apply to All Shortcuts
        </ButtonItem>
      </PanelSectionRow>
      {steamInputStatus && (
        <PanelSectionRow>
          <Field label={steamInputStatus} />
        </PanelSectionRow>
      )}
      {retroarchWarning?.warning && (
        <>
          <PanelSectionRow>
            <Field
              label={`RetroArch input_driver: "${retroarchWarning?.current}"`}
              description="Controller navigation in RetroArch menus may not work with this setting."
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={onFixInputDriver}
            >
              Fix input_driver to sdl2
            </ButtonItem>
          </PanelSectionRow>
          {retroarchFixStatus && (
            <PanelSectionRow>
              <Field label={retroarchFixStatus} />
            </PanelSectionRow>
          )}
        </>
      )}
    </PanelSection>
  );
};
