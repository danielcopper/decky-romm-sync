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

// Cached set of RomM app IDs — updated by registerRomMAppId / unregisterRomMAppId
const rommAppIds = new Set<number>();

// Tracks which appIds have already had their tree dumped (once per page load)
let treeDumped = false;

/**
 * Recursively walk a React element tree and log each node.
 * Useful for diagnosing tree structure changes after Steam updates.
 * Runs once per appId to avoid log spam on re-renders.
 */
function deepTreeDump(node: any, depth: number, index: number, prefix: string): void {
  if (depth > 5) return;
  if (node == null || typeof node !== "object") return;

  const indent = "  ".repeat(depth);
  const typeName =
    node?.type?.name ||
    node?.type?.displayName ||
    (typeof node?.type === "string" ? node.type : typeof node?.type === "function" ? "(anonymous fn)" : String(node?.type ?? "null"));
  const key = node?.key ?? "null";
  const className = (node?.props?.className || "").substring(0, 60) || "(none)";
  const childrenRaw = node?.props?.children;
  const childCount = Array.isArray(childrenRaw)
    ? childrenRaw.length
    : childrenRaw != null
    ? 1
    : 0;

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
function findInsertionPoint(ret: any): any {
  return findInReactTree(
    ret,
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
      (x: any) => x?.props?.className?.includes?.(bpsClass),
    );
    debugLog(`findInReactTree(basicAppDetailsSectionStylerClasses.PlaySection): ${bpsFound ? "FOUND" : "NOT FOUND"}`);
  }

  debugLog(`===== END DEEP TREE DUMP =====`);
}

export function registerRomMAppId(appId: number) {
  rommAppIds.add(appId);
}

export function unregisterRomMAppId(appId: number) {
  rommAppIds.delete(appId);
}

export function isRomMAppId(appId: number): boolean {
  return rommAppIds.has(appId);
}

let gamePatch: RoutePatch | null = null;

export function registerGameDetailPatch() {
  gamePatch = routerHook.addPatch(
    "/library/app/:appid",
    (tree: any) => {
      const routeProps = findInReactTree(tree, (x: any) => x?.renderFunc);
      if (routeProps) {
        const patchHandler = createReactTreePatcher(
          [
            // Navigate to the node whose children carry the overview prop
            (node: any) =>
              findInReactTree(
                node,
                (x: any) => x?.props?.children?.props?.overview,
              )?.props?.children,
          ],
          (_args: unknown[], ret?: any) => {
            const container = findInsertionPoint(ret);
            if (typeof container !== "object" || !container) {
              return ret;
            }

            // Extract appId from the overview object higher up in the tree
            const overviewNode = findInReactTree(
              ret,
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
