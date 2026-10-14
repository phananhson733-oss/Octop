import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentProvider, useAgent } from "./AgentContext";
import { CurrentUserProvider } from "../hooks/useCurrentUser";
import type { OctopUser } from "../api/modules/auth";

const api = vi.hoisted(() => ({ list: vi.fn() }));

vi.mock("../api/modules/agent", () => ({ agentApi: { list: api.list } }));
vi.mock("../api/modules/bridge", () => ({
  bridgeApi: { list: async () => [] },
}));

function Active() {
  const { activeAgentId, loading } = useAgent();
  return <span>{loading ? "loading" : `active:${activeAgentId}`}</span>;
}

function renderAs(user: Partial<OctopUser>) {
  render(
    <CurrentUserProvider user={user as OctopUser} setUser={() => undefined}>
      <AgentProvider>
        <Active />
      </AgentProvider>
    </CurrentUserProvider>,
  );
}

describe("AgentProvider default expert", () => {
  beforeEach(() => {
    localStorage.clear();
    api.list.mockResolvedValue([
      { agent_id: "A", state: "stopped", is_owner: true },
      {
        agent_id: "P",
        state: "running",
        is_owner: true,
        published_expert_id: "T1",
      },
    ]);
  });

  it("opens on the expert from the role's template when none is remembered", async () => {
    renderAs({ default_published_expert: "T1" });
    expect(await screen.findByText("active:P")).toBeInTheDocument();
    expect(localStorage.getItem("octop:active-agent")).toBe("P");
  });

  it("keeps the remembered expert", async () => {
    localStorage.setItem("octop:active-agent", "A");
    renderAs({ default_published_expert: "T1" });
    expect(await screen.findByText("active:A")).toBeInTheDocument();
  });

  it("opens on the first expert when the role names no template", async () => {
    renderAs({ default_published_expert: null });
    expect(await screen.findByText("active:A")).toBeInTheDocument();
  });
});
