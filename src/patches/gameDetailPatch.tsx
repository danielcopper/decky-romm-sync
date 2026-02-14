import { createElement } from "react";
import { routerHook } from "@decky/api";
import {
  afterPatch,
  findInReactTree,
  appDetailsClasses,
  createReactTreePatcher,
} from "@decky/ui";
import { GameDetailPanel } from "../components/GameDetailPanel";
import type { RoutePatch } from "@decky/api";

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

            // Avoid duplicate injection on re-render
            const alreadyInjected = container.props.children.some(
              (c: any) => c?.key === "romm-panel",
            );
            if (!alreadyInjected) {
              container.props.children.splice(
                1,
                0,
                createElement(GameDetailPanel, { appId, key: "romm-panel" }),
              );
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
