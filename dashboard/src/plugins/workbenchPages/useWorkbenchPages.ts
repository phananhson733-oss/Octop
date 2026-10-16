import { lazy, useEffect, useMemo, useSyncExternalStore } from "react";
import { pluginsApi } from "../../api/modules/plugins";
import { useCurrentUser } from "../../hooks/useCurrentUser";
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

/**
 * Workbench pages the current account may see; re-renders when plugin UIs load
 * or unload. ``adminOnly`` pages are left out until an admin is signed in.
 */
export function useWorkbenchPages(): WorkbenchPluginPage[] {
  const pages = useSyncExternalStore(
    subscribeWorkbenchPages,
    getWorkbenchPages,
    getWorkbenchPages,
  );
  const isAdmin = useCurrentUser()?.role === "admin";
  return useMemo(
    () => (isAdmin ? pages : pages.filter((page) => !page.adminOnly)),
    [pages, isAdmin],
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
      adminOnly: true,
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
