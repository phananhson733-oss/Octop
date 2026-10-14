import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import ExpertsPage from "./index";
import { CurrentUserProvider } from "../../hooks/useCurrentUser";
import type { OctopUser } from "../../api/modules/auth";

// The page's effects depend on ``t`` and ``agents``, so both keep one identity.
const stable = vi.hoisted(() => ({
  translation: { t: (key: string) => key, i18n: { language: "zh" } },
  agentContext: { agents: [], refresh: async () => undefined },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => stable.translation,
}));

// A drawer on the page pulls in the PDF preview, which needs a canvas.
vi.mock("react-pdf", () => ({
  Document: () => null,
  Page: () => null,
  pdfjs: { GlobalWorkerOptions: { workerSrc: "" } },
}));

vi.mock("../../api/request", () => ({
  request: vi.fn(async () => []),
}));

vi.mock("../../api/modules/publishedExperts", () => ({
  publishedExpertsApi: { list: vi.fn(async () => []) },
}));

vi.mock("../../context/AgentContext", () => ({
  useAgent: () => stable.agentContext,
}));

async function renderExperts(user: Partial<OctopUser>) {
  render(
    <MemoryRouter>
      <CurrentUserProvider user={user as OctopUser} setUser={() => undefined}>
        <ExpertsPage />
      </CurrentUserProvider>
    </MemoryRouter>,
  );
  // The empty guide appears once the page has settled on "no experts yet".
  await screen.findByText("experts.emptyGuideTitle");
}

function tab(labelKey: string) {
  return screen.queryByRole("tab", { name: new RegExp(labelKey) });
}

describe("Experts page tabs", () => {
  it("shows every tab when the role hides none", async () => {
    await renderExperts({ role: "user", permissions: [] });

    expect(tab("experts.myExperts")).toBeInTheDocument();
    expect(tab("experts.myTeams")).toBeInTheDocument();
    expect(tab("experts.expertLibrary")).toBeInTheDocument();
    expect(tab("experts.expertMarket")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "experts.goToLibrary" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("experts.emptyGuideStepTemplate"),
    ).toBeInTheDocument();
  });

  it("drops the tabs a role hides, and the links that lead to them", async () => {
    await renderExperts({
      role: "01ROLE",
      permissions: [],
      hidden_expert_tabs: ["teams", "library", "market"],
    });

    expect(tab("experts.myExperts")).toBeInTheDocument();
    expect(tab("experts.myTeams")).not.toBeInTheDocument();
    expect(tab("experts.expertLibrary")).not.toBeInTheDocument();
    expect(tab("experts.expertMarket")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "experts.goToLibrary" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("experts.emptyGuideStepTemplate"),
    ).not.toBeInTheDocument();
  });

  it("hides only the tabs the role names", async () => {
    await renderExperts({
      role: "01ROLE",
      permissions: [],
      hidden_expert_tabs: ["market"],
    });

    expect(tab("experts.myTeams")).toBeInTheDocument();
    expect(tab("experts.expertLibrary")).toBeInTheDocument();
    expect(tab("experts.expertMarket")).not.toBeInTheDocument();
  });
});
