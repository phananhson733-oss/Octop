import { useCallback, useEffect, useState } from "react";

/**
 * One open research result: a seed of a research run. Ported from OpenSEO
 * search-tabs (MIT): kept for the browser session per project, at most 20 with
 * the oldest evicted, and closing the active tab selects its right neighbor.
 * Tabs only point at runs already bought, so switching never buys anything.
 */
export interface SearchTab {
  id: string;
  runId: string;
  seed: string;
}

export interface SearchTabsState {
  tabs: SearchTab[];
  activeId: string | null;
}

export const SEARCH_TABS_LIMIT = 20;
const EMPTY: SearchTabsState = { tabs: [], activeId: null };

export function tabId(runId: string, seed: string): string {
  return JSON.stringify([runId, seed]);
}

/** Open ``entries`` (existing ones are reused) and select ``activate``. */
export function openTabs(
  state: SearchTabsState,
  entries: { runId: string; seed: string }[],
  activate: { runId: string; seed: string } | undefined = entries[0],
): SearchTabsState {
  let tabs = state.tabs;
  for (const entry of entries) {
    const id = tabId(entry.runId, entry.seed);
    if (tabs.some((tab) => tab.id === id)) continue;
    tabs = [
      ...tabs.slice(Math.max(0, tabs.length - SEARCH_TABS_LIMIT + 1)),
      { id, ...entry },
    ];
  }
  const activeId = activate ? tabId(activate.runId, activate.seed) : null;
  return {
    tabs,
    activeId: tabs.some((tab) => tab.id === activeId)
      ? activeId
      : state.activeId,
  };
}

export function closeTab(state: SearchTabsState, id: string): SearchTabsState {
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index === -1) return state;
  const tabs = state.tabs.filter((tab) => tab.id !== id);
  if (state.activeId !== id) return { tabs, activeId: state.activeId };
  const neighbor = tabs[index] ?? tabs[index - 1] ?? null;
  return { tabs, activeId: neighbor?.id ?? null };
}

export function parseStoredTabs(value: unknown): SearchTabsState {
  if (typeof value !== "object" || value === null) return EMPTY;
  const raw = (value as { tabs?: unknown; activeId?: unknown }).tabs;
  if (!Array.isArray(raw)) return EMPTY;
  const tabs = raw
    .flatMap((tab): SearchTab[] =>
      typeof tab?.runId === "string" &&
      tab.runId &&
      typeof tab.seed === "string" &&
      tab.seed
        ? [{ id: tabId(tab.runId, tab.seed), runId: tab.runId, seed: tab.seed }]
        : [],
    )
    .slice(-SEARCH_TABS_LIMIT);
  const activeId = (value as { activeId?: unknown }).activeId;
  return {
    tabs,
    activeId: tabs.some((tab) => tab.id === activeId)
      ? (activeId as string)
      : null,
  };
}

function load(key: string): SearchTabsState {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw ? parseStoredTabs(JSON.parse(raw)) : EMPTY;
  } catch {
    return EMPTY;
  }
}

export function useSearchTabs(projectId: string) {
  const key = `seo-search-tabs:${projectId}`;
  const [state, setState] = useState(() => load(key));
  useEffect(() => {
    try {
      window.sessionStorage.setItem(key, JSON.stringify(state));
    } catch {
      // Tabs still work in memory when sessionStorage is unavailable.
    }
  }, [key, state]);
  const open = useCallback(
    (
      entries: { runId: string; seed: string }[],
      activate?: { runId: string; seed: string },
    ) => setState((current) => openTabs(current, entries, activate)),
    [],
  );
  // Restored tabs win over the newest run on first load.
  const openIfEmpty = useCallback(
    (entries: { runId: string; seed: string }[]) =>
      setState((current) =>
        current.tabs.length ? current : openTabs(current, entries),
      ),
    [],
  );
  const select = useCallback(
    (id: string) =>
      setState((current) =>
        current.tabs.some((tab) => tab.id === id)
          ? { ...current, activeId: id }
          : current,
      ),
    [],
  );
  const close = useCallback(
    (id: string) => setState((current) => closeTab(current, id)),
    [],
  );
  return {
    ...state,
    active: state.tabs.find((tab) => tab.id === state.activeId) ?? null,
    open,
    openIfEmpty,
    select,
    close,
  };
}
