import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchPage from "./index";
import WorkbenchGate from "./WorkbenchGate";
import { CurrentUserProvider } from "../../../hooks/useCurrentUser";
import type { OctopUser } from "../../../api/modules/auth";
import {
  registerWorkbenchPage,
  resetWorkbenchPages,
  setPluginUisSettled,
} from "../../../plugins/workbenchPages/registry";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: "zh-CN" },
  }),
}));
vi.mock("../Terminal", () => ({ default: () => <div>terminal panel</div> }));
vi.mock("../RemoteBrowser", () => ({
  default: () => <div>browser panel</div>,
}));
vi.mock("../../../components/ForbiddenPage", () => ({
  default: () => <div>forbidden</div>,
}));
vi.mock("../../../layouts/PageShell", () => ({
  default: ({
    title,
    pathTabs,
    children,
  }: {
    title: string;
    pathTabs?: { options: { value: string }[] };
    children: React.ReactNode;
  }) => (
    <div>
      <h1>{title}</h1>
      <nav>{(pathTabs?.options ?? []).map((opt) => opt.value).join(",")}</nav>
      {children}
    </div>
  ),
}));

function Where() {
  return <output>{useLocation().pathname}</output>;
}

function renderAt(path: string, user: Partial<OctopUser>) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <CurrentUserProvider user={user as OctopUser} setUser={() => undefined}>
        <WorkbenchGate>
          <WorkbenchPage />
        </WorkbenchGate>
        <Where />
      </CurrentUserProvider>
    </MemoryRouter>,
  );
}

function addBoard() {
  registerWorkbenchPage({
    id: "board",
    pluginId: "pick",
    title: { zh: "选剧", en: "Picks" },
    component: () => <div>pick board</div>,
  });
}

function addAdminPage() {
  registerWorkbenchPage({
    id: "main",
    pluginId: "seo-workbench",
    title: "SEO",
    component: () => <div>seo page</div>,
    adminOnly: true,
  });
}

const member = { role: "01ROLE", permissions: [] };
const admin = { role: "admin", permissions: [] };

afterEach(() => {
  cleanup();
  resetWorkbenchPages();
  localStorage.clear();
});

describe("Workbench with plugin pages", () => {
  it("gives an account without terminal or browser only the plugin page", async () => {
    addBoard();
    setPluginUisSettled(true);
    renderAt("/workbench", member);

    expect(await screen.findByText("pick board")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "/workbench/pick.board",
    );
    expect(screen.getByRole("heading")).toHaveTextContent(
      "workbench.title / 选剧",
    );
    expect(screen.queryByText("terminal panel")).not.toBeInTheDocument();
    expect(screen.queryByText("browser panel")).not.toBeInTheDocument();
    expect(screen.getByRole("navigation")).toHaveTextContent("");
  });

  it("keeps terminal closed to that account", () => {
    addBoard();
    setPluginUisSettled(true);
    renderAt("/workbench/terminal", member);

    expect(screen.getByText("forbidden")).toBeInTheDocument();
    expect(screen.queryByText("terminal panel")).not.toBeInTheDocument();
  });

  it("denies the workbench when no plugin added a page", () => {
    setPluginUisSettled(true);
    renderAt("/workbench", member);

    expect(screen.getByText("forbidden")).toBeInTheDocument();
  });

  it("waits on a plugin page URL instead of redirecting before plugin UIs load", async () => {
    renderAt("/workbench/pick.board", admin);

    expect(screen.getByRole("status")).toHaveTextContent(
      "/workbench/pick.board",
    );
    expect(screen.queryByText("browser panel")).not.toBeInTheDocument();

    act(() => {
      addBoard();
      setPluginUisSettled(true);
    });

    expect(await screen.findByText("pick board")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "/workbench/pick.board",
    );
    expect(screen.getByRole("navigation")).toHaveTextContent(
      "browser,terminal,pick.board",
    );
    expect(screen.queryByText("browser panel")).not.toBeInTheDocument();
  });

  it("falls back to a built-in tab when the plugin page never registers", async () => {
    renderAt("/workbench/pick.board", admin);
    act(() => {
      setPluginUisSettled(true);
    });

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "/workbench/browser",
      ),
    );
    expect(screen.getByText("browser panel")).toBeInTheDocument();
  });
});

describe("Workbench with an admin-only page", () => {
  it("refuses its URL to a non-admin account", () => {
    addBoard();
    addAdminPage();
    setPluginUisSettled(true);
    renderAt("/workbench/seo-workbench.main", member);

    expect(screen.getByText("forbidden")).toBeInTheDocument();
    expect(screen.queryByText("seo page")).not.toBeInTheDocument();
  });

  it("does not open the workbench to a non-admin account on its own", () => {
    addAdminPage();
    setPluginUisSettled(true);
    renderAt("/workbench", member);

    expect(screen.getByText("forbidden")).toBeInTheDocument();
  });

  it("leaves it out of a non-admin account's tabs", async () => {
    addBoard();
    addAdminPage();
    setPluginUisSettled(true);
    renderAt("/workbench", { role: "01ROLE", permissions: ["terminal"] });

    expect(await screen.findByText("terminal panel")).toBeInTheDocument();
    expect(screen.getByRole("navigation")).toHaveTextContent(
      "terminal,pick.board",
    );
  });

  it("shows it to an admin", async () => {
    addAdminPage();
    setPluginUisSettled(true);
    renderAt("/workbench/seo-workbench.main", admin);

    expect(await screen.findByText("seo page")).toBeInTheDocument();
    expect(screen.getByRole("navigation")).toHaveTextContent(
      "browser,terminal,seo-workbench.main",
    );
  });
});
