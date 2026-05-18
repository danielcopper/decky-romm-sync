/**
 * React-tree patch that swaps Steam's native app-details overview panel for
 * our RomMPlaySection + RomMGameInfoPanel pair on RomM shortcuts. Everything
 * here walks Steam's internal React tree — node shapes are dynamic and shift
 * between Steam builds, so `any` is the honest type. Per #617, narrowing
 * predicates here buy almost no safety for several lines of churn per site,
 * so the file is exempt from `@typescript-eslint/no-explicit-any`.
 */

import { createElement } from "react";
import { routerHook } from "@decky/api";
import {
  afterPatch,
  findInReactTree,
  appDetailsClasses,
  createReactTreePatcher,
  playSectionClasses,
  basicAppDetailsSectionStylerClasses,
} from "@decky/ui";
import { RomMPlaySection } from "../components/RomMPlaySection";
import { RomMGameInfoPanel } from "../components/RomMGameInfoPanel";
import { debugLog } from "../api/backend";
import type { RoutePatch } from "@decky/api";

// Cached set of RomM app IDs — updated by registerRomMAppId
const rommAppIds = new Set<number>();

// Tracks which appIds have already had their tree dumped (once per page load)
let treeDumped = false;

/**
 * Best-effort display name for a React element's `type` field.
 * Anonymous function components surface as "(anonymous fn)" so they remain
 * distinguishable from the string fallback.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
function resolveTypeName(node: any): string {
  if (typeof node?.type === "string") return node.type;
  if (typeof node?.type === "function") return "(anonymous fn)";
  return String(node?.type ?? "null");
}

/**
 * Recursively walk a React element tree and log each node.
 * Useful for diagnosing tree structure changes after Steam updates.
 * Runs once per appId to avoid log spam on re-renders.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
function deepTreeDump(node: any, depth: number, index: number, prefix: string): void {
  if (depth > 5) return;
  if (node == null || typeof node !== "object") return;

  const indent = "  ".repeat(depth);
  const typeName =
    node?.type?.name ||
    node?.type?.displayName ||
    resolveTypeName(node);
  const key = node?.key ?? "null";
  const className = (node?.props?.className || "").substring(0, 60) || "(none)";
  const childrenRaw = node?.props?.children;
  let childCount: number;
  if (Array.isArray(childrenRaw)) {
    childCount = childrenRaw.length;
  } else if (childrenRaw == null) {
    childCount = 0;
  } else {
    childCount = 1;
  }

  debugLog(`${prefix}${indent}[${depth}:${index}] type=${typeName} key=${key} cls=${className} children=${childCount}`);

  // Recurse into children
  if (Array.isArray(childrenRaw)) {
    for (let i = 0; i < childrenRaw.length; i++) {
      deepTreeDump(childrenRaw[i], depth + 1, i, prefix);
    }
  } else if (childrenRaw != null && typeof childrenRaw === "object") {
    deepTreeDump(childrenRaw, depth + 1, 0, prefix);
  }
}

/**
 * Locate the InnerContainer node in Steam's app-details React subtree.
 * Returns the matched node (with an array `children` prop) or null/undefined.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
function findInsertionPoint(ret: any): any {
  return findInReactTree(
    ret,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
    (x: any) =>
      Array.isArray(x?.props?.children) &&
      x?.props?.className?.includes(appDetailsClasses.InnerContainer),
  );
}

/**
 * Locate the native AppDetailsOverviewPanel index among the InnerContainer's
 * children. Identified by children whose props carry details + overview +
 * bFastRender. Returns -1 if not found.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
function findNativeOverviewIndex(children: any[]): number {
  for (let i = 0; i < children.length; i++) {
    const cp = children[i]?.props?.children?.props || {};
    if (cp.details && cp.overview && cp.bFastRender !== undefined) {
      return i;
    }
  }
  return -1;
}

/**
 * One-time diagnostic dump of the InnerContainer subtree. Runs at most once
 * per plugin load (guarded by module-level `treeDumped`). No-op when the
 * appId is not a RomM shortcut.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
function dumpTree(container: any, appId: number): void {
  if (!rommAppIds.has(appId) || treeDumped) return;
  treeDumped = true;

  debugLog(`===== DEEP TREE DUMP for appId=${appId} =====`);
  debugLog(`InnerContainer className: ${container.props.className}`);

  const children = container.props.children;
  debugLog(`InnerContainer direct children count: ${children.length}`);
  for (let i = 0; i < children.length; i++) {
    deepTreeDump(children[i], 0, i, "TREE: ");
  }

  // Search for playSectionClasses.Container deep in tree
  const psContainerClass = playSectionClasses?.Container;
  debugLog(`playSectionClasses.Container = "${psContainerClass || "UNDEFINED"}"`);
  if (psContainerClass) {
    const psFound = findInReactTree(
      container,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
      (x: any) => x?.props?.className?.includes?.(psContainerClass),
    );
    debugLog(`findInReactTree(playSectionClasses.Container): ${psFound ? "FOUND" : "NOT FOUND"}`);
  }

  // Search for basicAppDetailsSectionStylerClasses.PlaySection deep in tree
  const bpsClass = basicAppDetailsSectionStylerClasses?.PlaySection;
  debugLog(`basicAppDetailsSectionStylerClasses.PlaySection = "${bpsClass || "UNDEFINED"}"`);
  if (bpsClass) {
    const bpsFound = findInReactTree(
      container,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
      (x: any) => x?.props?.className?.includes?.(bpsClass),
    );
    debugLog(`findInReactTree(basicAppDetailsSectionStylerClasses.PlaySection): ${bpsFound ? "FOUND" : "NOT FOUND"}`);
  }

  debugLog(`===== END DEEP TREE DUMP =====`);
}

export function registerRomMAppId(appId: number) {
  rommAppIds.add(appId);
}

export function isRomMAppId(appId: number): boolean {
  return rommAppIds.has(appId);
}

let gamePatch: RoutePatch | null = null;

export function registerGameDetailPatch() {
  gamePatch = routerHook.addPatch(
    "/library/app/:appid",
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
    (tree: any) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
      const routeProps = findInReactTree(tree, (x: any) => x?.renderFunc);
      if (routeProps) {
        const patchHandler = createReactTreePatcher(
          [
            // Navigate to the node whose children carry the overview prop
            // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
            (node: any) =>
              findInReactTree(
                node,
                // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
                (x: any) => x?.props?.children?.props?.overview,
              )?.props?.children,
          ],
          // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
          (_args: unknown[], ret?: any) => {
            const container = findInsertionPoint(ret);
            if (typeof container !== "object" || !container) {
              return ret;
            }

            // Extract appId from the overview object higher up in the tree
            const overviewNode = findInReactTree(
              ret,
              // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
              (x: any) => x?.props?.overview?.appid,
            );
            const appId: number | undefined =
              overviewNode?.props?.overview?.appid;

            if (!appId) {
              return ret;
            }

            // Only apply RomM modifications for RomM shortcuts
            const isRomM = rommAppIds.has(appId);
            debugLog(`gameDetailPatch: appId=${appId} isRomM=${isRomM} setSize=${rommAppIds.size}`);

            dumpTree(container, appId);

            // For RomM games: replace the native AppDetailsOverviewPanel
            // (which renders Play button + tabs + all native content via `se`)
            // with our RomMPlaySection and RomMGameInfoPanel.
            if (isRomM) {
              const children = container.props.children;

              // Deduplication: don't inject if already present
              const alreadyHasPlayBtn = children.some(
                // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Steam internal React tree; runtime shape is dynamic, no upstream types ship
                (c: any) => c?.key === "romm-play-section",
              );
              if (!alreadyHasPlayBtn) {
                const nativeOverviewIdx = findNativeOverviewIndex(children);

                const rommPlaySection = createElement(RomMPlaySection, {
                  key: "romm-play-section",
                  appId,
                });

                const rommInfoPanel = createElement(RomMGameInfoPanel, {
                  key: "romm-info-panel",
                  appId,
                });

                // Wrap in a container with the native AppDetailsOverviewPanel
                // CSS class so it participates in InnerContainer's flex layout
                // and scroll system the same way the native panel does.
                const rommWrapper = createElement("div", {
                  key: "romm-play-section",
                  className: appDetailsClasses?.AppDetailsOverviewPanel || "",
                  "data-romm": "true",
                }, rommPlaySection, rommInfoPanel);

                if (nativeOverviewIdx >= 0) {
                  debugLog(`gameDetailPatch: replacing AppDetailsOverviewPanel at index ${nativeOverviewIdx} with RomM wrapper (cls=${appDetailsClasses?.AppDetailsOverviewPanel})`);
                  children.splice(nativeOverviewIdx, 1, rommWrapper);
                } else {
                  debugLog(`gameDetailPatch: AppDetailsOverviewPanel not found, inserting RomM wrapper at index 1`);
                  children.splice(1, 0, rommWrapper);
                }
              }
            }

            return ret;
          },
          "RomMGameDetail",
        );

        afterPatch(routeProps, "renderFunc", patchHandler);
      }

      return tree;
    },
  );
}

export function unregisterGameDetailPatch() {
  if (gamePatch) {
    routerHook.removePatch("/library/app/:appid", gamePatch);
    gamePatch = null;
  }
}
