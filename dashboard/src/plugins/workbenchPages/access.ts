import { canAccessPath, type PermissionHolder } from "../../utils/permissions";
import { BUILTIN_WORKBENCH_TABS } from "./registry";

export type WorkbenchAccess = "allow" | "deny" | "pending";

/** First path segment under ``/workbench/``, or null on the bare path. */
export function workbenchTabOf(pathname: string): string | null {
  if (!pathname.startsWith("/workbench/")) return null;
  return pathname.slice("/workbench/".length).split("/")[0] || null;
}

/**
 * Who may open a Workbench URL.
 *
 * Terminal and browser keep their module permissions. Pages added by plugins
 * are open to every signed-in account (their files are already served to any
 * signed-in account), so an account with neither permission still gets the
 * Workbench when a plugin page exists — and only that page.
 */
export function workbenchAccess(
  user: PermissionHolder | null | undefined,
  pathname: string,
  pageSlugs: readonly string[],
  settled: boolean,
): WorkbenchAccess {
  if (!user) return "pending";
  if (canAccessPath(user, pathname)) return "allow";
  const tab = workbenchTabOf(pathname);
  if (tab && (BUILTIN_WORKBENCH_TABS as readonly string[]).includes(tab)) {
    return "deny";
  }
  if (tab ? pageSlugs.includes(tab) : pageSlugs.length > 0) return "allow";
  return settled ? "deny" : "pending";
}
