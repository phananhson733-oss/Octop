/** Path → sidebar key. Kept free of the route table so any component can import it. */

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
