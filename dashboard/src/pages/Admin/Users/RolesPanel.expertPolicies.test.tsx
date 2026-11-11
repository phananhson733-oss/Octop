import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserRole } from "../../../api/modules/userRoles";

// The panel reloads when ``t`` changes, so it keeps one identity.
const stable = vi.hoisted(() => ({
  translation: { t: (key: string) => key, i18n: { language: "zh" } },
  request: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => stable.translation,
}));

// The panel's imports reach the PDF preview, which needs a canvas.
vi.mock("react-pdf", () => ({
  Document: () => null,
  Page: () => null,
  pdfjs: { GlobalWorkerOptions: { workerSrc: "" } },
}));

vi.mock("../../../api/request", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/request")>()),
  request: stable.request,
}));

vi.mock("@/utils/antdMessage", () => ({
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

import { message } from "@/utils/antdMessage";
import RolesPanel from "./RolesPanel";

const pickRole: UserRole = {
  user_role_id: "01ROLE",
  user_role_name: "选剧角色",
  description: null,
  system_role: "user",
  permissions: ["knowledge_bases"],
  policies: [
    { name: "allowed_plugin_ui", value: '["sab-tools"]' },
    { name: "allowed_expert_templates", value: '["01TEMPLATE"]' },
    { name: "visible_nav", value: "chat,experts" },
    { name: "hidden_expert_tabs", value: "library,market" },
    { name: "default_published_expert", value: "01TEMPLATE,01SEO" },
  ],
  deletable: true,
  immutable: false,
  workspace_root_dir: null,
  token_quota: null,
  max_agents: null,
  visible_nav: ["chat", "experts"],
  default_knowledge_bases: [],
  hidden_expert_tabs: ["library", "market"],
  default_published_experts: ["01TEMPLATE", "01SEO"],
  created_at: 1,
  updated_at: 1,
};

describe("<RolesPanel /> expert policies", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stable.request.mockReset();
    stable.request.mockImplementation(async (path: string) => {
      if (path === "/plugins") return [{ id: "sab-tools", name: "客户选剧" }];
      if (path === "/users/roles") return [pickRole];
      if (path === "/experts/published") {
        return [
          { id: "01TEMPLATE", name: "选剧专家模板" },
          { id: "01SEO", name: "SEO 专家模板" },
        ];
      }
      // The save names no new template here, but the server may report installs.
      if (path === "/users/roles/01ROLE") {
        return { ...pickRole, default_expert_installed: 2 };
      }
      if (path === "/filesystem/defaults") {
        return { tree_root: "/", browse_roots: [], in_container: true };
      }
      return [];
    });
  });

  it("shows the role's expert policies and saves them with its other policies", async () => {
    const user = userEvent.setup();
    render(<RolesPanel />);

    const row = (await screen.findByText("选剧角色")).closest("tr");
    expect(row).not.toBeNull();
    await user.click(
      within(row as HTMLElement).getByRole("button", { name: "common.edit" }),
    );

    const field = (
      await screen.findByText("adminUsers.roleHiddenExpertTabs")
    ).closest(".ant-form-item");
    expect(field).not.toBeNull();
    const chosen = within(field as HTMLElement);
    expect(chosen.getByText("experts.expertLibrary")).toBeInTheDocument();
    expect(chosen.getByText("experts.expertMarket")).toBeInTheDocument();
    expect(chosen.queryByText("experts.myTeams")).not.toBeInTheDocument();

    const template = screen
      .getByText("adminUsers.roleDefaultPublishedExpert")
      .closest(".ant-form-item");
    // A role can name several templates.
    expect(
      await within(template as HTMLElement).findByText("选剧专家模板"),
    ).toBeInTheDocument();
    expect(
      within(template as HTMLElement).getByText("SEO 专家模板"),
    ).toBeInTheDocument();

    expect(screen.getByText("客户选剧 (sab-tools)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "common.save" }));

    await waitFor(() =>
      expect(stable.request).toHaveBeenCalledWith(
        "/users/roles/01ROLE",
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
    const patch = stable.request.mock.calls.find(
      ([path]) => path === "/users/roles/01ROLE",
    );
    expect(JSON.parse(patch?.[1].body).policies).toEqual([
      { name: "visible_nav", value: "chat,experts" },
      { name: "hidden_expert_tabs", value: "library,market" },
      // In the order chosen: the first one is the expert chat opens on.
      { name: "default_published_expert", value: "01TEMPLATE,01SEO" },
      { name: "allowed_plugin_ui", value: '["sab-tools"]' },
      { name: "allowed_expert_templates", value: '["01TEMPLATE"]' },
    ]);
    // The toast says how many existing accounts just got the expert.
    await waitFor(() =>
      expect(message.success).toHaveBeenCalledWith(
        "adminUsers.roleDefaultExpertInstalled",
      ),
    );
  });
  it("saves an explicit deny-all policy when the last plugin is removed", async () => {
    const user = userEvent.setup();
    render(<RolesPanel />);
    const row = (await screen.findByText("选剧角色")).closest("tr");
    await user.click(
      within(row as HTMLElement).getByRole("button", { name: "common.edit" }),
    );
    const field = (await screen.findByText("adminUsers.rolePluginUi")).closest(
      ".ant-form-item",
    );
    const remove = (field as HTMLElement).querySelector(
      ".ant-select-selection-item-remove",
    );
    expect(remove).not.toBeNull();
    await user.click(remove as HTMLElement);
    await user.click(screen.getByRole("button", { name: "common.save" }));
    await waitFor(() =>
      expect(stable.request).toHaveBeenCalledWith(
        "/users/roles/01ROLE",
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
    const patch = stable.request.mock.calls.find(
      ([path]) => path === "/users/roles/01ROLE",
    );
    expect(JSON.parse(patch?.[1].body).policies).toContainEqual({
      name: "allowed_plugin_ui",
      value: "[]",
    });
  });

  it("keeps a workbench-only role without chat when it is saved", async () => {
    const workbenchRole: UserRole = {
      ...pickRole,
      permissions: [],
      policies: [
        { name: "allowed_plugin_ui", value: '["sab-tools"]' },
        { name: "visible_nav", value: "workbench" },
      ],
      visible_nav: ["workbench"],
      hidden_expert_tabs: [],
      default_published_experts: [],
    };
    const fallback = stable.request.getMockImplementation();
    stable.request.mockImplementation(async (path: string, init?: unknown) =>
      path === "/users/roles" ? [workbenchRole] : fallback?.(path, init),
    );
    const user = userEvent.setup();
    render(<RolesPanel />);
    const row = (await screen.findByText("选剧角色")).closest("tr");
    await user.click(
      within(row as HTMLElement).getByRole("button", { name: "common.edit" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "common.save" }),
    );
    await waitFor(() =>
      expect(stable.request).toHaveBeenCalledWith(
        "/users/roles/01ROLE",
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
    const patch = stable.request.mock.calls.find(
      ([path]) => path === "/users/roles/01ROLE",
    );
    expect(JSON.parse(patch?.[1].body).policies).toContainEqual({
      name: "visible_nav",
      value: "workbench",
    });
  });
});
