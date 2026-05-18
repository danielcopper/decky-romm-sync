/**
 * Read-only list of devices currently registered with the RomM save-sync
 * backend. Visible only when save-sync is enabled; parent owns the device
 * list, loading flag, and error message.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, Field } from "@decky/ui";
import type { RegisteredDevice } from "../../api/backend";
import { formatRelativeTime } from "./helpers";

interface RegisteredDevicesSectionProps {
  devicesLoading: boolean;
  devicesError: string | null;
  registeredDevices: RegisteredDevice[] | null;
}

export const RegisteredDevicesSection: FC<RegisteredDevicesSectionProps> = ({
  devicesLoading,
  devicesError,
  registeredDevices,
}) => {
  return (
    <PanelSection title="Registered Devices">
      {devicesLoading && (
        <PanelSectionRow>
          <Field label="Loading..." />
        </PanelSectionRow>
      )}
      {!devicesLoading && devicesError && (
        <PanelSectionRow>
          <Field label="Could not load devices" description={devicesError} />
        </PanelSectionRow>
      )}
      {!devicesLoading && !devicesError && registeredDevices !== null && registeredDevices.length === 0 && (
        <PanelSectionRow>
          <Field label="No devices registered" />
        </PanelSectionRow>
      )}
      {!devicesLoading && !devicesError && registeredDevices !== null && registeredDevices.map((device, i) => {
        const parts: string[] = [
          `${device.client ?? "unknown client"} v${device.client_version ?? "?"}`,
          ...(device.platform ? [device.platform] : []),
          `last seen ${formatRelativeTime(device.last_seen)}`,
          `ID ${String(device.id ?? "").slice(0, 8) || "—"}`,
        ];
        return (
          <PanelSectionRow key={device.id || `idx-${i}`}>
            <Field
              label={
                <span>
                  {device.name ?? "(unnamed)"}
                  {device.is_current_device && (
                    <span style={{ color: "#6ab04c", marginLeft: "8px", fontSize: "12px" }}>(this device)</span>
                  )}
                </span>
              }
              description={parts.join(" · ")}
            />
          </PanelSectionRow>
        );
      })}
    </PanelSection>
  );
};
