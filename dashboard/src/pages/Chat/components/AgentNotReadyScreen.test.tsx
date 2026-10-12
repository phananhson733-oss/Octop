import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import AgentNotReadyScreen from "./AgentNotReadyScreen";
import { CurrentUserProvider } from "../../../hooks/useCurrentUser";
import type { OctopUser } from "../../../api/modules/auth";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

function renderNoAgents(user: Partial<OctopUser>) {
  render(
    <MemoryRouter>
      <CurrentUserProvider user={user as OctopUser} setUser={() => undefined}>
        <AgentNotReadyScreen agent={null} noAgents />
      </CurrentUserProvider>
    </MemoryRouter>,
  );
}

describe("AgentNotReadyScreen without experts", () => {
  it("offers to create an expert when the Experts page is visible", () => {
    renderNoAgents({ role: "user", permissions: [] });

    expect(screen.getByText("chat.noAgentsHint")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "chat.createExpert" }),
    ).toBeInTheDocument();
  });

  it("points to an administrator when the role hides the Experts page", () => {
    renderNoAgents({ role: "01ROLE", permissions: [], visible_nav: ["chat"] });

    expect(screen.getByText("chat.askAdminForExpert")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "chat.createExpert" }),
    ).not.toBeInTheDocument();
  });
});
