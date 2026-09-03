import { useState, useEffect, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  Focusable,
  ConfirmModal,
  showContextMenu,
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
import type { FirmwarePlatformExt, FirmwareWanted } from "../types";
import { scrollToTop } from "../utils/scrollHelpers";
import { biosColorForLevel } from "../utils/biosColor";
import { biosFileNote } from "../utils/biosFileNote";
import { detach } from "../utils/detach";
import { getEventTarget } from "../utils/events";
import { buildEmulatorMenu } from "../utils/emulatorMenu";
import {
  capturePruneLeaseAdmission,
  mountPruneLeaseOwner,
  releasePruneLeasesByOwner,
  withPruneLease,
} from "../utils/pruneLease";
import { batchConfirmLaunchOptions } from "../utils/launchOptionsReconcile";

/**
 * How each of the four `wanted` values reads on a file row. `not_needed` is
 * spelled out rather than shortened: "not needed" is a statement about every
 * installed emulator, and the row beside it saying "unknown" is the absence of
 * one, so the two must not look like near-synonyms.
 */
const WANTED_LABELS: Record<FirmwareWanted, string> = {
  needed: "needed",
  optional: "optional",
  not_needed: "not needed",
  unknown: "unknown",
};

/**
 * Build the per-platform summary label/description from the backend BIOS
 * aggregates. The ok/partial/missing DECISION is the backend's `bios_level`
 * (`compute_bios_level`) — `requiredReady` is `bios_level === "ok"`, so the
 * required-files threshold is no longer re-compared here. `requiredCount` still
 * selects the phrasing axis (required vs. plain file counts), and the
 * optional-missing breakdown stays a local computation passed in by the caller.
 *
 * With nothing required, the library ratio is inventory and is worded as such —
 * the same framing the BIOS tab uses. "0 / 20 files … 20 missing" over twenty
 * files no installed core asks for reads as work outstanding on a system that
 * needs nothing.
 */
function getBiosSummary(
  requiredCount: number,
  requiredDone: number,
  requiredReady: boolean,
  optionalMissing: number,
  done: number,
  total: number,
) {
  if (requiredCount > 0 && requiredReady) {
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
    summaryLabel: "Nothing required",
    summaryDescription: total > 0 ? `${done} / ${total} files held` : "No BIOS files in your library",
  };
}

/**
 * The summary for a platform making no readiness claim. Two shapes reach it and
 * they are different sentences.
 *
 * `requiredWithheld` above zero is a platform whose emulators DID answer and one
 * of whose required rows nothing could judge — a declared folder the resolver
 * could not read, say. Zero is the older shape: no installed emulator's
 * answer could be established for the platform at all, which splits again on
 * whether there are rows to point at, because a platform whose emulators are all
 * standalone has none and "0 file(s) nothing installed could answer for" would
 * read as a finished count of nothing rather than as silence.
 */
function getUnknownSummary(requiredWithheld: number, total: number) {
  if (requiredWithheld > 0) {
    return {
      summaryLabel: "BIOS readiness unknown",
      summaryDescription:
        requiredWithheld === 1
          ? "A required file could not be judged — see the file list"
          : `${requiredWithheld} required files could not be judged — see the file list`,
    };
  }
  return {
    summaryLabel: "BIOS requirement unknown",
    summaryDescription:
      total > 0
        ? `${total} file(s) nothing installed could answer for`
        : "Nothing installed could answer for this system",
  };
}

/**
 * Tell an open game-detail page that this platform's firmware changed, so it
 * re-reads its BIOS requirement instead of leaving the pre-change one standing
 * (#939). Every download and delete on this page is such a change; nothing else
 * on the page is.
 *
 * Call it only when firmware actually changed. The event fans out to every
 * mounted panel and each one that matches the slug pays a live
 * `check_platform_bios` for it (#1082), so a run that moved no files must stay
 * silent rather than send an event no panel can act on.
 */
function announceBiosChange(platformSlug: string) {
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", { detail: { type: "bios", platform_slug: platformSlug } }),
  );
}

/**
 * Thin horizontal rule dividing adjacent platform blocks. Rows within one
 * platform carry `bottomSeparator="none"`, so this is the only divider — one
 * line between neighbouring platforms, none between the rows of a platform.
 */
const PlatformSeparator: FC = () => (
  <PanelSectionRow>
    {/* marginTop offsets the built-in top lead of the following PanelSection
        title (the block below the rule) so the line sits centred in the gap
        rather than hugging the block above it. */}
    <div
      data-testid="platform-separator"
      style={{ height: "1px", marginTop: "14px", backgroundColor: "rgba(255, 255, 255, 0.12)" }}
    />
  </PanelSectionRow>
);

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
  const leaseOwner = "system-page";
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
    mountPruneLeaseOwner(leaseOwner);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async data loads on mount are the standard React pattern; the rule is overzealous here
    detach(refreshSystem());
    return () => {
      detach(releasePruneLeasesByOwner(leaseOwner));
    };
  }, []);

  const handleDownloadAll = async (platformSlug: string) => {
    setDownloading(platformSlug);
    setBiosStatus("");
    try {
      const result = await downloadAllFirmware(platformSlug);
      if (result.success) {
        setBiosStatus(result.message || `Downloaded ${result.downloaded} files`);
        await refreshSystem();
        if ((result.downloaded ?? 0) > 0) announceBiosChange(platformSlug);
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
        if ((result.downloaded ?? 0) > 0) announceBiosChange(platformSlug);
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
        announceBiosChange(platformSlug);
      }
    } catch (e) {
      setBiosStatus(`Failed to delete BIOS files: ${e}`);
    }
  };

  const confirmDeleteBios = (platformSlug: string) => {
    showModal(
      <ConfirmModal
        strTitle={`Delete BIOS files for ${platformSlug}?`}
        strDescription="This deletes only the BIOS files this plugin downloaded for this system. Files your emulator came with, or that you put there yourself, are left where they are. Games that need the deleted files won't launch until you download them again."
        strOKButtonText="Delete BIOS Files"
        strCancelButtonText="Cancel"
        onOK={() => {
          detach(handleDeleteBios(platformSlug));
        }}
      />,
    );
  };

  const handleSystemCoreChange = async (platform: FirmwarePlatformExt, pickedLabel: string) => {
    // Picking the default-marked emulator clears the per-platform override
    // (empty label → follow the es_systems default); any other pins it.
    const defaultEmulator = platform.emulators?.find((e) => e.is_default);
    const label = pickedLabel === defaultEmulator?.label ? "" : pickedLabel;
    detach(debugLog(`setSystemCore: slug=${platform.platform_slug} label=${label} (selected=${pickedLabel})`));
    try {
      const admission = capturePruneLeaseAdmission(leaseOwner);
      const result = await setSystemCore(platform.platform_slug, label);
      detach(debugLog(`setSystemCore: result success=${result.success}`));
      if (result.success) {
        // Re-bake launch_options for every affected installed ROM on this
        // platform. The backend returns the fresh command per bound shortcut;
        // confirm-set each so existing shortcuts launch with the new core.
        // Mirrors the migration_relaunch_options fan-out in index.tsx
        // (bounded-concurrency batches so a platform with many ROMs doesn't
        // serialize worst-case per-shortcut confirm-poll timeouts).
        await withPruneLease(
          result.prune_lease_token,
          "setSystemCore",
          (signal) => batchConfirmLaunchOptions(result.rebake_items ?? [], "setSystemCore", signal),
          leaseOwner,
          admission,
        );
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

  // The per-platform emulator picker shares the game-detail menu builder
  // (utils/emulatorMenu, #1210). No "Use System Override" reset item and no
  // "(system)" marker here — this page IS the system level, so picking the
  // default-marked emulator is itself the clear-to-empty-label action.
  const showSystemCoreMenu = (platform: FirmwarePlatformExt, e: Event) => {
    showContextMenu(
      buildEmulatorMenu({
        emulators: platform.emulators ?? [],
        emulatorDataAvailable: platform.emulator_data_available ?? true,
        activeLabel: platform.active_core_label ?? null,
        platformCoreLabel: null,
        onPick: (label) => {
          detach(handleSystemCoreChange(platform, label));
        },
      }),
      getEventTarget(e),
    );
  };

  // Only currently-synced systems are shown (#956): a platform counts as synced
  // when it has at least one ROM bound to a Steam shortcut (has_games).
  const syncedPlatforms = biosPlatforms.filter((p) => p.has_games);

  const renderBiosPlatform = (platform: FirmwarePlatformExt, index: number) => {
    const isLastPlatform = index === syncedPlatforms.length - 1;
    const unansweredFiles = platform.files.filter((f) => f.wanted === "unknown");
    // Display counts come from the backend aggregates (computed from the same
    // core-aware files); fall back to local derivation only if a payload omits
    // them. `total` is the LIBRARY's file count, not the row count — the rows
    // include files no library holds, and a progress ratio over those would
    // report work the user cannot do. The expander below names the row count
    // instead, because that is the list it opens. The optional-missing
    // breakdown stays a local file-level axis — the level doesn't model it.
    const total = platform.server_count ?? platform.files.filter((f) => f.on_server).length;
    const done = platform.local_count ?? platform.files.filter((f) => f.on_server && f.downloaded).length;
    const allDone = done === total;
    // What Delete BIOS would remove — a record count, not a library one. There
    // is no local fallback: the rows say nothing about who downloaded a file,
    // so a payload without the field offers no delete rather than guessing.
    const deletable = platform.deletable_count ?? 0;
    const isDownloading = downloading === platform.platform_slug;
    const isExpanded = expanded[platform.platform_slug] ?? false;

    const requiredFiles = platform.files.filter((f) => f.required_by_active);
    const requiredCount = platform.required_count ?? requiredFiles.length;
    const requiredDone = platform.required_downloaded ?? requiredFiles.filter((f) => f.downloaded).length;
    const optionalMissing = platform.files.filter(
      (f) => f.wanted === "optional" && !f.required_by_active && !f.downloaded,
    ).length;

    // The ok/partial/missing DECISION is the backend's bios_level — "ready"
    // means all required files present (bios_level === "ok"). Fall back to the
    // local count comparison only when the level is absent from the payload.
    const requiredReady = platform.bios_level == null ? requiredDone === requiredCount : platform.bios_level === "ok";

    // "unknown": the plugin makes no readiness claim. Render neutral grey +
    // honest text instead of a false all-clear — and never a "BIOS needed" flag
    // either, since that is a claim in the other direction.
    //
    // Two shapes reach it and they are different sentences. `requiredWithheld`
    // above zero is a platform whose emulators DID answer and one of whose
    // required rows nothing could judge — a declared folder the resolver could
    // not read, say. Everything else here was answered, so the rows below
    // stand and so do the downloads. Zero is the older shape: no installed
    // emulator's answer could be established for this platform at all. Its
    // description splits again on whether there are rows to point at, because a
    // platform whose emulators are all standalone has none, and "0 file(s)
    // nothing installed could answer for" would read as a finished count of
    // nothing rather than as silence.
    const isUnknown = platform.bios_level === "unknown";
    const requiredWithheld = platform.required_withheld ?? 0;
    const nothingEstablished = isUnknown && requiredWithheld === 0;

    const needsAttention = platform.has_games && !isUnknown && requiredCount > 0 && !requiredReady;
    const { summaryLabel, summaryDescription } = isUnknown
      ? getUnknownSummary(requiredWithheld, total)
      : getBiosSummary(requiredCount, requiredDone, requiredReady, optionalMissing, done, total);
    // The download affordances key off what is missing AND fetchable, never off
    // readiness: a required file the RomM library does not hold leaves the
    // platform not ready and still gives the user nothing to press here.
    //
    // `nothingEstablished` withdraws them entirely, and that is a PLATFORM
    // condition, never a per-file one: a platform whose reading finished may
    // hold plenty of files no installed emulator asks for — a PlayStation page
    // typically does — and every one of them stays fetchable, because "nothing
    // wants this" is an answer.
    // Where nothing could be established there is no answer to download
    // against, so the page says so instead of offering to fetch files it cannot
    // reason about. A declined READINESS verdict is not that state and keeps its
    // buttons: its rows were answered, and downloading the files the library
    // holds is the one thing that can still move the platform along. Nothing
    // here is keyed to a platform name — the condition is the backend's verdict,
    // so a system starts offering downloads again the moment anything can speak
    // for it.
    //
    // A folder declaration is out whatever its state: the emulator lists that
    // name, so there is no file to fetch into it — what would satisfy it is a
    // BIOS image inside the folder, which is a different row.
    const fetchableMissing = nothingEstablished
      ? []
      : platform.files.filter((f) => f.on_server && !f.downloaded && f.declared_kind !== "directory");
    const hasRequiredMissing = fetchableMissing.some((f) => f.required_by_active);
    const hasOptionalMissing = fetchableMissing.some((f) => !f.required_by_active);

    const hasMultipleCores = !!platform.emulators && platform.emulators.length > 1;

    return (
      <PanelSection
        key={platform.platform_slug}
        title={`${platform.platform_slug}${needsAttention ? " — BIOS needed" : ""}`}
      >
        {/* Emulator core selection is the primary per-system concern (#923),
            shown above the BIOS file management. The picker opens the shared
            context menu so libretro AND standalone emulators (and disabled
            un-bakeable entries with their reason) render identically to the
            game-detail menu (#1210). */}
        {hasMultipleCores && (
          <PanelSectionRow>
            <ButtonItem layout="below" bottomSeparator="none" onClick={(e: Event) => showSystemCoreMenu(platform, e)}>
              {`Emulator Core: ${platform.active_core_label ?? "Default"}`}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {platform.active_core_label && !hasMultipleCores && (
          <PanelSectionRow>
            <Field label="Emulator Core" description={platform.active_core_label} bottomSeparator="none" />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <Field
            label={
              isUnknown ? (
                <span style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                  <span
                    style={{
                      display: "inline-block",
                      width: "8px",
                      height: "8px",
                      borderRadius: "50%",
                      backgroundColor: biosColorForLevel(platform.bios_level ?? null),
                      flexShrink: 0,
                    }}
                  />
                  {summaryLabel}
                </span>
              ) : (
                summaryLabel
              )
            }
            description={summaryDescription}
            bottomSeparator="none"
          />
        </PanelSectionRow>
        {/* Withdrawing the download buttons without a word would read as "there
            is nothing to fetch", which is the finished answer this platform
            precisely does not have. It says which of the two it is, and what the
            user can still do — the files are theirs to place, the plugin just
            cannot say which ones are wanted. */}
        {nothingEstablished && (
          <PanelSectionRow>
            <div style={{ fontSize: "11px", color: "#8f98a0", padding: "0 16px 4px" }}>
              BIOS management is not supported for this system yet, so there is nothing to download here. You can still
              put BIOS files in your BIOS folder by hand.
            </div>
          </PanelSectionRow>
        )}
        {/* A platform with no rows at all is on the page because its requirement
            is unknown, not because there is a list to open. */}
        {platform.files.length > 0 && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              bottomSeparator="none"
              onClick={() =>
                setExpanded((prev) => ({
                  ...prev,
                  [platform.platform_slug]: !prev[platform.platform_slug],
                }))
              }
            >
              {isExpanded ? "Hide Files" : `Show Files (${platform.files.length})`}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {isExpanded && (
          <Focusable>
            {platform.files.map((file) => {
              let dotColor: string;
              // The row's VERDICT, not `downloaded`: for a declared folder the
              // two come apart, since the folder is there on every RetroDECK
              // install and what satisfies the core is a file inside it. A
              // payload with no verdict falls back to `downloaded`, which is
              // what the verdict is for a plain file.
              const verdict = file.satisfied === undefined ? file.downloaded : file.satisfied;
              // A row nothing could judge is amber for the same reason a row
              // nothing could be asked about is.
              if (file.wanted === "unknown" || verdict === null) {
                dotColor = "#d4a72c";
              } else if (verdict) {
                dotColor = "#5ba32b";
              } else if (file.required_by_active) {
                dotColor = "#d94126";
              } else {
                dotColor = "#8f98a0";
              }
              // The provenance/kind note is shared with the BIOS tab so one row
              // cannot read two ways; plain absence is this page's own word,
              // because the tab leaves that to its dot.
              const note = biosFileNote(file) || (file.downloaded ? "" : "Missing");
              return (
                <PanelSectionRow key={file.file_name}>
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
                        {`${file.description || file.file_name} (${WANTED_LABELS[file.wanted]})`}
                      </span>
                    }
                    description={note ? `${file.file_name} — ${note}` : file.file_name}
                    bottomSeparator="none"
                  />
                </PanelSectionRow>
              );
            })}
            {unansweredFiles.length > 0 && (
              <PanelSectionRow>
                <Field
                  label={`${unansweredFiles.length} file(s) nothing installed could answer for`}
                  description="Report at github.com/danielcopper/romm-tender/issues if needed."
                  bottomSeparator="none"
                />
              </PanelSectionRow>
            )}
          </Focusable>
        )}
        {hasRequiredMissing && !serverOffline && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              bottomSeparator="none"
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
              bottomSeparator="none"
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
            at least one file it would actually remove. That number is the
            backend's `deletable_count` — the plugin's own download records that
            are still on disk, which is exactly what the delete unlinks. The
            library ratio (`done`) counts a different set and was wrong here in
            both directions, including hiding the button over downloads RomM had
            stopped listing. Destructive → ConfirmModal. */}
        {deletable > 0 && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              bottomSeparator="none"
              onClick={() => confirmDeleteBios(platform.platform_slug)}
              disabled={isDownloading}
            >
              {`Delete BIOS (${deletable})`}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {!isLastPlatform && <PlatformSeparator />}
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
        {/* General note about core switching — shown once at the top, not per
            platform, since the caveat is the same for every system (#938). */}
        <PanelSectionRow>
          <div style={{ fontSize: "11px", color: "#ffb74d", padding: "0 16px 4px" }}>
            Switching cores may affect save compatibility
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

        {!biosLoading && !biosError && syncedPlatforms.length === 0 && (
          <PanelSectionRow>
            <Field label="No synced systems" description="Sync some games to manage their cores and BIOS files here." />
          </PanelSectionRow>
        )}

        {biosStatus && (
          <PanelSectionRow>
            <Field label={biosStatus} />
          </PanelSectionRow>
        )}
        {/* Close the System intro block off from the first platform with the same
            divider that sits between platforms — so a line precedes every platform
            block, none trails the last. Only when platforms actually follow. */}
        {syncedPlatforms.length > 0 && <PlatformSeparator />}
      </PanelSection>

      {syncedPlatforms.map(renderBiosPlatform)}
    </>
  );
};
