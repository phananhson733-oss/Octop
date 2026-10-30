import { lazy, useEffect, useMemo, useSyncExternalStore } from "react";
import { pluginsApi } from "../../api/modules/plugins";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { userCan } from "../../utils/permissions";
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
 * or unload. A page with a ``permission`` is left out for accounts without it.
 */
export function useWorkbenchPages(): WorkbenchPluginPage[] {
  const pages = useSyncExternalStore(
    subscribeWorkbenchPages,
    getWorkbenchPages,
    getWorkbenchPages,
  );
  const user = useCurrentUser();
  return useMemo(
    () =>
      pages.filter(
        (page) => !page.permission || userCan(user, page.permission),
      ),
    [pages, user],
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
        en: "Keywords, competitors, site audit, rankings and AI visibility",
        zh: "关键词、竞争对手、技术审计、排名与 AI 可见度",
      },
      component: lazy(() => import("../../pages/Control/SeoWorkbench")),
      permission: "seo",
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
