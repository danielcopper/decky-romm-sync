import { useState, useEffect, FC, createElement } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  Focusable,
  DropdownItem,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import {
  getFirmwareStatus,
  downloadAllFirmware,
  downloadRequiredFirmware,
  deletePlatformBios,
  setSystemCore,
  debugLog,
} from "../api/backend";
import type { FirmwarePlatformExt } from "../types";
import { scrollToTop } from "../utils/scrollHelpers";
import { detach } from "../utils/detach";

function getBiosSummary(
  requiredCount: number,
  requiredDone: number,
  allRequiredDone: boolean,
  optionalMissing: number,
  done: number,
  total: number,
  allDone: boolean,
) {
  if (requiredCount > 0 && allRequiredDone) {
    return {
      summaryLabel: `${requiredDone} / ${requiredCount} required`,
      summaryDescription:
        optionalMissing > 0 ? `All required ready (${optionalMissing} optional missing)` : "All required ready",
    };
  }
  if (requiredCount > 0) {
    return {
      summaryLabel: `${requiredDone} / ${requiredCount} required`,
      summaryDescription: `${requiredCount - requiredDone} required missing — games may not launch`,
    };
  }
  return {
    summaryLabel: `${done} / ${total} files`,
    summaryDescription: allDone ? "All downloaded" : `${total - done} missing`,
  };
}

function hashIndicator(hv: boolean | null): string {
  if (hv === true) return " ✓";
  if (hv === false) return " ⚠";
  return " —";
}

interface SystemPageProps {
  onBack: () => void;
}

/**
 * Top-level QAM destination for per-system emulator configuration: the active
 * emulator core and the BIOS files that core needs, per platform. Core data
 * comes from the `get_firmware_status` multi-platform overview, which carries
 * both the active/available cores and the BIOS file state for every platform in
 * one call.
 */
export const SystemPage: FC<SystemPageProps> = ({ onBack }) => {
  const [biosPlatforms, setBiosPlatforms] = useState<FirmwarePlatformExt[]>([]);
  const [biosLoading, setBiosLoading] = useState(true);
  const [biosError, setBiosError] = useState("");
  const [serverOffline, setServerOffline] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [biosStatus, setBiosStatus] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  async function refreshSystem() {
    setBiosLoading(true);
    setBiosError("");
    try {
      const result = await getFirmwareStatus();
      if (result.success) {
        setBiosPlatforms(result.platforms);
        setServerOffline(result.server_offline ?? false);
      } else {
        setBiosError(result.message || "Failed to fetch firmware status");
      }
    } catch (e) {
      setBiosError(`Failed to fetch firmware status: ${e}`);
    }
    setBiosLoading(false);
  }

  // Load System data (core + BIOS) on mount — this page IS the System view.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async data loads on mount are the standard React pattern; the rule is overzealous here
    detach(refreshSystem());
  }, []);

  const handleDownloadAll = async (platformSlug: string) => {
    setDownloading(platformSlug);
    setBiosStatus("");
    try {
      const result = await downloadAllFirmware(platformSlug);
      if (result.success) {
        setBiosStatus(result.message || `Downloaded ${result.downloaded} files`);
        await refreshSystem();
      } else {
        setBiosStatus(result.message || "Download failed");
      }
    } catch (e) {
      setBiosStatus(`Download failed: ${e}`);
    }
    setDownloading(null);
  };

  const handleDownloadRequired = async (platformSlug: string) => {
    setDownloading(platformSlug);
    setBiosStatus("");
    try {
      const result = await downloadRequiredFirmware(platformSlug);
      if (result.success) {
        setBiosStatus(result.message || `Downloaded ${result.downloaded} required files`);
        await refreshSystem();
      } else {
        setBiosStatus(result.message || "Download failed");
      }
    } catch (e) {
      setBiosStatus(`Download failed: ${e}`);
    }
    setDownloading(null);
  };

  // Destructive action — deletes the platform's downloaded BIOS files. Mirrors
  // the DangerZone confirm UX (ConfirmModal via showModal). Kept flat at the
  // component-body level (like handleDownloadAll) so the modal's onOK is a
  // single named-handler call rather than a deeply-nested async closure (S2004).
  const handleDeleteBios = async (platformSlug: string) => {
    setBiosStatus("");
    try {
      const result = await deletePlatformBios(platformSlug);
      setBiosStatus(result.message);
      if (result.success) {
        await refreshSystem();
      }
    } catch (e) {
      setBiosStatus(`Failed to delete BIOS files: ${e}`);
    }
  };

  const confirmDeleteBios = (platformSlug: string) => {
    showModal(
      createElement(ConfirmModal, {
        strTitle: `Delete BIOS files for ${platformSlug}?`,
        strDescription:
          "This deletes every downloaded BIOS file for this system from your RetroDECK bios directory. Games that need these files won't launch until you download them again.",
        strOKButtonText: "Delete BIOS Files",
        strCancelButtonText: "Cancel",
        onOK: () => {
          detach(handleDeleteBios(platformSlug));
        },
      }),
    );
  };

  const handleSystemCoreChange = async (platform: FirmwarePlatformExt, optionData: string) => {
    const defaultCore = platform.available_cores?.find((c) => c.is_default);
    const label = optionData === defaultCore?.label ? "" : optionData;
    detach(debugLog(`setSystemCore: slug=${platform.platform_slug} label=${label} (selected=${optionData})`));
    try {
      const result = await setSystemCore(platform.platform_slug, label);
      detach(debugLog(`setSystemCore: result success=${result.success}`));
      if (result.success) {
        await refreshSystem();
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: platform.platform_slug },
          }),
        );
      }
    } catch (e) {
      detach(debugLog(`setSystemCore: error: ${e}`));
    }
  };

  const withGames = biosPlatforms.filter((p) => p.has_games);
  const withoutGames = biosPlatforms.filter((p) => !p.has_games);

  const renderBiosPlatform = (platform: FirmwarePlatformExt) => {
    const total = platform.files.length;
    const done = platform.files.filter((f) => f.downloaded).length;
    const allDone = done === total;
    const isDownloading = downloading === platform.platform_slug;
    const isExpanded = expanded[platform.platform_slug] ?? false;

    const requiredFiles = platform.files.filter((f) => f.classification === "required");
    const unknownFiles = platform.files.filter((f) => f.classification === "unknown");
    const requiredCount = requiredFiles.length;
    const requiredDone = requiredFiles.filter((f) => f.downloaded).length;
    const allRequiredDone = requiredDone === requiredCount;
    const optionalMissing = platform.files.filter((f) => f.classification === "optional" && !f.downloaded).length;

    const needsAttention = platform.has_games && !allRequiredDone;
    const { summaryLabel, summaryDescription } = getBiosSummary(
      requiredCount,
      requiredDone,
      allRequiredDone,
      optionalMissing,
      done,
      total,
      allDone,
    );
    const hasRequiredMissing = requiredCount > 0 && !allRequiredDone;
    const hasOptionalMissing = optionalMissing > 0;

    const hasMultipleCores = !!platform.available_cores && platform.available_cores.length > 1;

    return (
      <PanelSection
        key={platform.platform_slug}
        title={`${platform.platform_slug}${needsAttention ? " — BIOS needed" : ""}`}
      >
        {/* Emulator core selection is the primary per-system concern (#923),
            shown above the BIOS file management. */}
        {hasMultipleCores && (
          <>
            <PanelSectionRow>
              <DropdownItem
                label="Emulator Core"
                rgOptions={[
                  ...platform.available_cores!.map((c) => ({
                    data: c.label,
                    label: c.is_default ? `${c.label} (default)` : c.label,
                  })),
                ]}
                selectedOption={
                  platform.active_core_label || platform.available_cores!.find((c) => c.is_default)?.label || ""
                }
                onChange={(option: { data: string }) => detach(handleSystemCoreChange(platform, option.data))}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ fontSize: "11px", color: "#ffb74d", padding: "0 16px 4px" }}>
                Switching cores may affect save compatibility
              </div>
            </PanelSectionRow>
          </>
        )}
        {platform.active_core_label && !hasMultipleCores && (
          <PanelSectionRow>
            <Field label="Emulator Core" description={platform.active_core_label} />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <Field label={summaryLabel} description={summaryDescription} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() =>
              setExpanded((prev) => ({
                ...prev,
                [platform.platform_slug]: !prev[platform.platform_slug],
              }))
            }
          >
            {isExpanded ? "Hide Files" : `Show Files (${total})`}
          </ButtonItem>
        </PanelSectionRow>
        {isExpanded && (
          <Focusable>
            {platform.files.map((file) => {
              let dotColor: string;
              if (file.classification === "unknown") {
                dotColor = "#d4a72c";
              } else if (file.downloaded) {
                dotColor = "#5ba32b";
              } else if (file.classification === "required") {
                dotColor = "#d94126";
              } else {
                dotColor = "#8f98a0";
              }
              return (
                <PanelSectionRow key={file.id}>
                  <Field
                    label={
                      <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                        <span
                          style={{
                            display: "inline-block",
                            width: "8px",
                            height: "8px",
                            borderRadius: "50%",
                            backgroundColor: dotColor,
                            flexShrink: 0,
                          }}
                        />
                        {`${file.description || file.file_name} (${file.classification})`}
                      </span>
                    }
                    description={
                      file.downloaded
                        ? `${file.file_name}${hashIndicator(file.hash_valid)}`
                        : `${file.file_name} — Missing`
                    }
                  />
                </PanelSectionRow>
              );
            })}
            {unknownFiles.length > 0 && (
              <PanelSectionRow>
                <Field
                  label={`${unknownFiles.length} file(s) not recognized`}
                  description="Report at github.com/danielcopper/decky-romm-sync/issues if needed."
                />
              </PanelSectionRow>
            )}
          </Focusable>
        )}
        {hasRequiredMissing && !serverOffline && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => {
                detach(handleDownloadRequired(platform.platform_slug));
              }}
              disabled={isDownloading}
            >
              {isDownloading ? "Downloading..." : "Download Required"}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {!allDone && (hasOptionalMissing || hasRequiredMissing) && !serverOffline && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => {
                detach(handleDownloadAll(platform.platform_slug));
              }}
              disabled={isDownloading}
            >
              {isDownloading ? "Downloading..." : "Download All"}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {/* Delete is local-only (no server needed) and shown only when there is
            at least one downloaded file to delete. Destructive → ConfirmModal. */}
        {done > 0 && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => confirmDeleteBios(platform.platform_slug)}
              disabled={isDownloading}
            >
              {`Delete BIOS (${done})`}
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
    );
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={onBack}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
          >
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="System">
        <PanelSectionRow>
          <div style={{ fontSize: "11px", color: "#8f98a0", padding: "0 16px 4px" }}>
            Per-system emulator core and BIOS files. The active core determines which BIOS files a system needs.
          </div>
        </PanelSectionRow>
        {biosLoading && (
          <PanelSectionRow>
            <Field label="Loading firmware status..." />
          </PanelSectionRow>
        )}

        {biosError && (
          <PanelSectionRow>
            <Field label="Error" description={biosError} />
          </PanelSectionRow>
        )}

        {serverOffline && (
          <PanelSectionRow>
            <Field
              label="Server offline"
              description="RomM server is unreachable. Downloads unavailable, but core switching still works."
            />
          </PanelSectionRow>
        )}

        {!biosLoading && !biosError && biosPlatforms.length === 0 && (
          <PanelSectionRow>
            <Field label="No firmware files found" />
          </PanelSectionRow>
        )}

        {biosStatus && (
          <PanelSectionRow>
            <Field label={biosStatus} />
          </PanelSectionRow>
        )}
      </PanelSection>

      {withGames.map(renderBiosPlatform)}
      {withoutGames.map(renderBiosPlatform)}
    </>
  );
};
