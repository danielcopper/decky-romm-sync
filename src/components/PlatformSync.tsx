import { useState, useEffect, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ToggleField,
  Spinner,
} from "@decky/ui";
import {
  getPlatforms,
  savePlatformSync,
  setAllPlatformsSync,
} from "../api/backend";
import type { PlatformSyncSetting } from "../types";

interface PlatformSyncProps {
  onBack: () => void;
}

export const PlatformSync: FC<PlatformSyncProps> = ({ onBack }) => {
  const [platforms, setPlatforms] = useState<PlatformSyncSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    getPlatforms()
      .then((result) => {
        if (result.success) {
          setPlatforms(result.platforms);
        } else {
          setError(true);
        }
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, []);

  const handleToggle = async (id: number, enabled: boolean) => {
    setPlatforms((prev) =>
      prev.map((p) => (p.id === id ? { ...p, sync_enabled: enabled } : p))
    );
    try {
      await savePlatformSync(id, enabled);
    } catch {
      setPlatforms((prev) =>
        prev.map((p) => (p.id === id ? { ...p, sync_enabled: !enabled } : p))
      );
    }
  };

  const handleSetAll = async (enabled: boolean) => {
    const previous = platforms.map((p) => ({ ...p }));
    setPlatforms((prev) => prev.map((p) => ({ ...p, sync_enabled: enabled })));
    try {
      await setAllPlatformsSync(enabled);
    } catch {
      setPlatforms(previous);
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
      <PanelSection title="Platforms">
        {loading ? (
          <PanelSectionRow>
            <Spinner />
          </PanelSectionRow>
        ) : error ? (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={onBack}>
              Failed to load platforms
            </ButtonItem>
          </PanelSectionRow>
        ) : (
          <>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => handleSetAll(true)}>
                Enable All
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => handleSetAll(false)}>
                Disable All
              </ButtonItem>
            </PanelSectionRow>
            {platforms.map((platform) => (
              <PanelSectionRow key={platform.id}>
                <ToggleField
                  label={platform.name}
                  description={`${platform.rom_count} ROMs`}
                  checked={platform.sync_enabled}
                  onChange={(value: boolean) =>
                    handleToggle(platform.id, value)
                  }
                />
              </PanelSectionRow>
            ))}
          </>
        )}
      </PanelSection>
    </>
  );
};
