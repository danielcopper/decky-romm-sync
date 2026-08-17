/**
 * GameInfoTab — the GAME INFO pane of the RomM game detail panel: the
 * descriptive rows about the game, and the ROM File section underneath.
 *
 * Render only, from props — nothing here fetches. The panel streams state in as
 * its background reads land, so this pane is routinely open before the cover,
 * the installed-rom record and a stale metadata refresh arrive.
 *
 * Uses createElement (no JSX) to match the panel. CSS classes prefixed with
 * `romm-panel-` are injected separately by styleInjector.
 */

import { FC, createElement } from "react";
import type { InstalledRom, RomMetadata } from "../types";
import { infoRow, section } from "./panelSection";

interface GameInfoTabProps {
  /** The RomM game name (distinct from the Steam shortcut hero title, which can differ). */
  romName: string;
  /** Region / Languages of the active version (ADR-0021) — an empty list hides its row. */
  regions: string[];
  languages: string[];
  metadata: RomMetadata | null;
  platformName: string;
  coverBase64: string | null;
  installed: boolean;
  installedRom: InstalledRom | null;
}

/** Format a Unix timestamp (seconds) as a release date string (e.g. "15 Mar 2003") */
function formatReleaseDate(timestamp: number | null): string | null {
  if (!timestamp || timestamp <= 0) return null;
  const date = new Date(timestamp * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${date.getDate()} ${months[date.getMonth()]} ${date.getFullYear()}`;
}

/** The rows a RomM metadata record contributes, in display order. */
function metadataRows(meta: RomMetadata, platformName: string): ReturnType<typeof createElement>[] {
  const rows: ReturnType<typeof createElement>[] = [];

  if (meta.summary) {
    rows.push(createElement("div", { key: "summary", className: "romm-panel-summary" }, meta.summary));
  }

  if (platformName) {
    rows.push(infoRow("platform", "Platform", platformName));
  }

  if (meta.companies.length > 0) {
    rows.push(infoRow("companies", "Developer / Publisher", meta.companies.join(", ")));
  }

  if (meta.genres.length > 0) {
    rows.push(
      createElement(
        "div",
        { key: "genres", className: "romm-panel-info-row" },
        createElement("span", { className: "romm-panel-label" }, "Genres"),
        createElement(
          "div",
          { className: "romm-panel-tags" },
          ...meta.genres.map((g) => createElement("span", { key: g, className: "romm-panel-tag" }, g)),
        ),
      ),
    );
  }

  const releaseDate = formatReleaseDate(meta.first_release_date);
  if (releaseDate) {
    rows.push(infoRow("release-date", "Release Date", releaseDate));
  }

  if (meta.game_modes.length > 0) {
    rows.push(infoRow("game-modes", "Game Modes", meta.game_modes.join(", ")));
  }

  if (meta.player_count) {
    rows.push(infoRow("players", "Players", meta.player_count));
  }

  if (meta.average_rating != null && meta.average_rating > 0) {
    rows.push(infoRow("rating", "Rating", `${Math.round(meta.average_rating)}%`));
  }

  return rows;
}

/** Every descriptive row the pane shows, in display order. */
function gameInfoRows(props: GameInfoTabProps): ReturnType<typeof createElement>[] {
  const rows: ReturnType<typeof createElement>[] = [];

  if (props.romName) {
    rows.push(createElement("div", { key: "rom-name", className: "romm-panel-rom-name" }, props.romName));
  }

  if (props.regions.length > 0) {
    rows.push(infoRow("regions", "Region", props.regions.join("/")));
  }
  if (props.languages.length > 0) {
    rows.push(infoRow("languages", "Languages", props.languages.join(", ")));
  }

  if (props.metadata) {
    rows.push(...metadataRows(props.metadata, props.platformName));
  } else if (props.platformName) {
    // No metadata — still show platform
    rows.push(infoRow("platform", "Platform", props.platformName));
  }

  // "No metadata available" fires only when NO descriptive row was added (name,
  // region/languages, metadata, or platform) — a plain count of the rows above.
  if (rows.length === 0) {
    rows.push(createElement("div", { key: "no-meta", className: "romm-panel-muted" }, "No metadata available"));
  }

  return rows;
}

/** The ROM File section — present only once the ROM is on disk. */
function romFileSection(
  installed: boolean,
  installedRom: InstalledRom | null,
): ReturnType<typeof createElement> | null {
  if (!installed || !installedRom) return null;

  // A downloaded ROM the system cannot launch keeps its files and its row; only
  // the shortcut's launch command is withheld. This is the one place that says
  // so — without it the game simply never starts and nothing explains why.
  const noLaunchTargetNote = installedRom.launchable
    ? null
    : createElement(
        "div",
        { key: "no-launch-target", className: "romm-panel-muted", style: { marginTop: "4px" } },
        `Downloaded, but nothing here is a format ${installedRom.system} can launch — no launch ` +
          `command was set. The files are on disk; install them in the emulator to play.`,
      );

  return section("rom-file", "ROM File", infoRow("filename", "Filename", installedRom.file_name), noLaunchTargetNote);
}

export const GameInfoTab: FC<GameInfoTabProps> = (props) => {
  const rows = gameInfoRows(props);

  const gameInfoSection = props.coverBase64
    ? section(
        "game-info",
        null,
        createElement(
          "div",
          {
            key: "game-info-row",
            style: { display: "flex", gap: "16px", alignItems: "flex-start" },
          },
          createElement("img", {
            key: "cover",
            src: `data:image/png;base64,${props.coverBase64}`,
            style: { width: "120px", borderRadius: "4px", flexShrink: 0, objectFit: "cover" as const },
          }),
          createElement("div", { key: "details", style: { flex: 1 } }, ...rows),
        ),
      )
    : section("game-info", null, ...rows);

  return createElement("div", null, gameInfoSection, romFileSection(props.installed, props.installedRom));
};
