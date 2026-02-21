import { createElement } from "react";
import { routerHook } from "@decky/api";
import {
  afterPatch,
  findInReactTree,
  appDetailsClasses,
  playSectionClasses,
  createReactTreePatcher,
} from "@decky/ui";
import { CustomPlayButton } from "../components/CustomPlayButton";
import { setBypassBypass } from "./metadataPatches";
import { debugLog } from "../api/backend";
import type { RoutePatch } from "@decky/api";

// Cached set of RomM app IDs — updated by registerRomMAppId / unregisterRomMAppId
const rommAppIds = new Set<number>();

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
            // Find the InnerContainer by its CSS class
            const container = findInReactTree(
              ret,
              (x: any) =>
                Array.isArray(x?.props?.children) &&
                x?.props?.className?.includes(appDetailsClasses.InnerContainer),
            );

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

            // Set bypass counter so BIsModOrShortcut returns false during
            // this render pass, enabling metadata sections for our shortcuts.
            if (isRomM) {
              setBypassBypass(11);
            }

            // For RomM games: inject CustomPlayButton into InnerContainer
            // after the native PlaySection
            if (isRomM) {
              const children = container.props.children;

              // Deduplication: don't insert if already present
              const alreadyHasPlayBtn = children.some(
                (c: any) => c?.key === "romm-play-section",
              );
              if (!alreadyHasPlayBtn) {
                // Find the native PlaySection by matching playSectionClasses.Container
                let insertIdx = -1;
                for (let i = 0; i < children.length; i++) {
                  const child = children[i];
                  if (
                    child?.props?.className &&
                    typeof child.props.className === "string" &&
                    playSectionClasses?.Container &&
                    child.props.className.includes(playSectionClasses.Container)
                  ) {
                    insertIdx = i + 1;
                    break;
                  }
                }
                // Fallback: insert at position 1 if PlaySection not found
                if (insertIdx < 0) insertIdx = 1;

                debugLog(`gameDetailPatch: inserting CustomPlayButton at index ${insertIdx}`);

                children.splice(
                  insertIdx,
                  0,
                  createElement("div", {
                    key: "romm-play-section",
                    "data-romm": "true",
                    style: {
                      display: "flex",
                      alignItems: "center",
                      width: "100%",
                      padding: "16px 2.8vw",
                      boxSizing: "border-box",
                      background: "rgba(14, 20, 27, 0.33)",
                      position: "relative",
                    },
                  },
                    createElement(CustomPlayButton, { appId }),
                  ),
                );
              } else {
                // Position correction: ensure our element stays at the right spot
                const currentIdx = children.findIndex(
                  (c: any) => c?.key === "romm-play-section",
                );
                if (currentIdx >= 0) {
                  let expectedIdx = -1;
                  for (let i = 0; i < children.length; i++) {
                    if (i === currentIdx) continue;
                    const child = children[i];
                    if (
                      child?.props?.className &&
                      typeof child.props.className === "string" &&
                      playSectionClasses?.Container &&
                      child.props.className.includes(playSectionClasses.Container)
                    ) {
                      expectedIdx = i + 1;
                      break;
                    }
                  }
                  if (expectedIdx >= 0 && currentIdx !== expectedIdx) {
                    const [elem] = children.splice(currentIdx, 1);
                    // Adjust index if we removed before the target
                    const adjustedIdx = currentIdx < expectedIdx ? expectedIdx - 1 : expectedIdx;
                    children.splice(adjustedIdx, 0, elem);
                  }
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
