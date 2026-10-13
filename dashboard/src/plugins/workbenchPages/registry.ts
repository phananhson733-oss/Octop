import type {
  WorkbenchPageRegistration,
  WorkbenchPageTitle,
} from "../toolRenderers/types";

/** Workbench tabs that ship with the dashboard; a plugin page can never take these URL segments. */
export const BUILTIN_WORKBENCH_TABS = ["browser", "terminal"] as const;

export interface WorkbenchPluginPage extends WorkbenchPageRegistration {
  /** URL segment under ``/workbench/`` and the tab value. */
  slug: string;
}

let pages: WorkbenchPluginPage[] = [];
let settled = false;
const listeners = new Set<() => void>();

function emit(): void {
  for (const fn of listeners) {
    try {
      fn();
    } catch {
      /* ignore */
    }
  }
}

export function workbenchPageSlug(pluginId: string, id: string): string {
  return `${pluginId}.${id}`;
}

export function subscribeWorkbenchPages(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Stable array identity between changes, so it works with ``useSyncExternalStore``. */
export function getWorkbenchPages(): WorkbenchPluginPage[] {
  return pages;
}

export function registerWorkbenchPage(reg: WorkbenchPageRegistration): void {
  const slug = workbenchPageSlug(reg.pluginId, reg.id);
  if (!/^[\w.-]+$/.test(slug)) {
    console.warn(`[plugin-ui] workbench page id not URL-safe: ${slug}`);
    return;
  }
  pages = [...pages.filter((page) => page.slug !== slug), { ...reg, slug }];
  emit();
}

export function unregisterWorkbenchPages(pluginId: string): void {
  const next = pages.filter((page) => page.pluginId !== pluginId);
  if (next.length === pages.length) return;
  pages = next;
  emit();
}

/**
 * True once the app-level plugin UI load has finished (or failed). Until then
 * the list of plugin pages is not final, so the Workbench must not redirect
 * away from, or deny, a plugin page URL.
 */
export function arePluginUisSettled(): boolean {
  return settled;
}

export function setPluginUisSettled(value: boolean): void {
  if (settled === value) return;
  settled = value;
  emit();
}

export function workbenchPageTitle(
  title: WorkbenchPageTitle,
  language: string,
): string {
  if (typeof title === "string") return title;
  const lang = language.toLowerCase().startsWith("zh") ? "zh" : "en";
  return title[lang] ?? title.en ?? title.zh ?? Object.values(title)[0] ?? "";
}

/** Test helper. */
export function resetWorkbenchPages(): void {
  pages = [];
  settled = false;
  emit();
}
