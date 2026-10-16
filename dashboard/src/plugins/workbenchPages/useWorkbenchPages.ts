import { lazy, useEffect, useSyncExternalStore } from "react";
import { pluginsApi } from "../../api/modules/plugins";
import { loadInstalledPluginUis } from "../toolRenderers/loader";
import { updateToolPluginIndex } from "../toolRenderers/toolPluginIndex";
import {
  registerWorkbenchPage,
  arePluginUisSettled,
  getWorkbenchPages,
  setPluginUisSettled,
  subscribeWorkbenchPages,
  type WorkbenchPluginPage,
} from "./registry";

/** Pages plugins added to the Workbench; re-renders when plugin UIs load or unload. */
export function useWorkbenchPages(): WorkbenchPluginPage[] {
  return useSyncExternalStore(
    subscribeWorkbenchPages,
    getWorkbenchPages,
    getWorkbenchPages,
  );
}

/** False while the first plugin UI load is still running. */
export function usePluginUisSettled(): boolean {
  return useSyncExternalStore(
    subscribeWorkbenchPages,
    arePluginUisSettled,
    arePluginUisSettled,
  );
}

let started = false;

/**
 * Load installed plugin UIs once per app session from the layout, so pages a
 * plugin adds to the Workbench show up in the sidebar without opening a chat.
 */
export function useInstalledPluginUis(enabled: boolean): void {
  useEffect(() => {
    if (!enabled || started) return;
    started = true;
    registerWorkbenchPage({
      pluginId: "seo-workbench",
      id: "main",
      title: { en: "SEO", zh: "SEO" },
      description: {
        en: "Keyword research, saved keywords and private reports",
        zh: "关键词研究、保存关键词与私有报告",
      },
      component: lazy(() => import("../../pages/Control/SeoWorkbench")),
    });
    (async () => {
      try {
        const plugins = await pluginsApi.list();
        updateToolPluginIndex(plugins);
        await loadInstalledPluginUis(plugins);
      } catch (err) {
        console.warn("[plugin-ui] list/load failed:", err);
      } finally {
        setPluginUisSettled(true);
      }
    })();
  }, [enabled]);
}
