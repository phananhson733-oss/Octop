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

import RolesPanel from "./RolesPanel";

const pickRole: UserRole = {
  user_role_id: "01ROLE",
  user_role_name: "选剧角色",
  description: null,
  system_role: "user",
  permissions: ["knowledge_bases"],
  policies: [
    { name: "visible_nav", value: "chat,experts" },
    { name: "hidden_expert_tabs", value: "library,market" },
  ],
  deletable: true,
  immutable: false,
  workspace_root_dir: null,
  token_quota: null,
  max_agents: null,
  visible_nav: ["chat", "experts"],
  default_knowledge_bases: [],
  hidden_expert_tabs: ["library", "market"],
  created_at: 1,
  updated_at: 1,
};

describe("<RolesPanel /> hidden expert tabs", () => {
  beforeEach(() => {
    stable.request.mockReset();
    stable.request.mockImplementation(async (path: string) => {
      if (path === "/users/roles") return [pickRole];
      if (path === "/filesystem/defaults") {
        return { tree_root: "/", browse_roots: [], in_container: true };
      }
      return [];
    });
  });

  it("shows the role's hidden tabs and saves them with its other policies", async () => {
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
    ]);
  });
});
