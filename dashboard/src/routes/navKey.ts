/** Path → sidebar key. Kept free of the route table so any component can import it. */

import { canAccessPath, type PermissionHolder } from "../utils/permissions";

export const pathToKey: Record<string, string> = {
  "/chat": "chat",
  // Common
  "/experts": "experts",
  "/tasks": "tasks",
  "/connectors": "connectors",
  "/skill-packages": "skill-packages",
  "/knowledge-bases": "knowledge-bases",
  "/bridge": "bridge",
  "/personalization": "personalization",
  "/personalization/skills": "personalization",
  "/personalization/tools": "personalization",
  "/personalization/plugins": "personalization",
  "/personalization/subagents": "personalization",
  "/personalization/channels": "channels",
  "/personalization/mbti": "personalization",
  "/personalization/memory": "personalization",
  "/skills": "personalization",
  "/token-usage": "token-usage",
  "/agent-config": "agent-config",
  // Control
  "/acp": "acp",
  "/channels": "channels",
  "/workbench": "workbench",
  "/workbench/terminal": "workbench",
  "/workbench/browser": "workbench",
  "/terminal": "workbench",
  "/remote-browser": "workbench",
  "/remote-desktop": "remote-desktop",
  "/remote-desktop/desktop": "remote-desktop",
  "/remote-desktop/phone": "remote-desktop",
  "/remote-desktop/phone/screen": "remote-desktop",
  "/remote-desktop/phone/shell": "remote-desktop",
  "/remote-phone": "remote-desktop",
  "/remote-android": "remote-desktop",
  "/subagents": "personalization",
  "/mbti": "personalization",
  "/memory": "personalization",
  // Admin
  "/admin/models": "models",
  // Admin
  "/admin/users": "admin-users",
  "/admin/backend": "admin-storage",
  "/admin/plugins": "admin-plugins",
  "/admin/advanced": "admin-advanced",
  "/admin/security": "admin-security",
};

export function resolveSelectedKey(pathname: string): string {
  if (pathToKey[pathname]) return pathToKey[pathname];
  if (pathname.startsWith("/chat/")) return "chat";
  if (pathname.startsWith("/workbench/")) return "workbench";
  if (pathname.startsWith("/remote-desktop/")) return "remote-desktop";
  if (pathname.startsWith("/personalization/")) return "personalization";
  return "";
}

/**
 * Where an account lands: the chat page, or the first page the role keeps
 * when it hides chat (``null`` when none of its pages has a path or opens).
 * Whether the Workbench opens is the caller's to say: plugin pages there need
 * no module permission, so it depends on which plugin UIs loaded.
 */
export function homePath(
  user: PermissionHolder | null | undefined,
  workbenchOpen = false,
): string | null {
  const visibleNav = user?.visible_nav;
  if (!visibleNav || visibleNav.includes("chat")) return "/chat";
  const paths = Object.keys(pathToKey);
  for (const key of visibleNav) {
    if (key === "workbench") {
      if (workbenchOpen) return "/workbench";
      continue;
    }
    // A tab, not its parent: a bare parent reopens the last tab, which may
    // belong to a key the role hides (/personalization → .../channels).
    const path = paths.find(
      (p) =>
        pathToKey[p] === key &&
        !paths.some((q) => q.startsWith(p + "/")) &&
        canAccessPath(user, p),
    );
    if (path) return path;
  }
  return null;
}
