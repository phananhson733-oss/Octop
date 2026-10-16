import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SeoWorkbench from "./index";
import { seoApi, type ResearchRun } from "../../../api/modules/seo";
import { octopThreadsApi } from "../../../api/modules/octopThreads";
import { staticReportHtml } from "./reportPreview";

const translate = vi.hoisted(
  () => (key: string, values?: Record<string, unknown>) =>
    key === "seo.reportPrompt"
      ? `Report for ${values?.projectId}/${values?.runId}`
      : key,
);
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock("../../../api/modules/seo", () => ({
  seoApi: {
    projects: vi.fn(),
    provider: vi.fn(),
    createProject: vi.fn(),
    configureProvider: vi.fn(),
    runs: vi.fn(),
    research: vi.fn(),
    keywords: vi.fn(),
    saveKeywords: vi.fn(),
    reports: vi.fn(),
    report: vi.fn(),
  },
}));
vi.mock("../../../api/modules/octopThreads", () => ({
  octopThreadsApi: { create: vi.fn() },
}));
vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({
    agents: [{ agent_id: "agent-one", name: "My SEO", is_owner: true }],
  }),
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 1 }),
}));
vi.mock("../../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname} {JSON.stringify(location.state)}
    </output>
  );
}
const row = {
  keyword: "zero volume",
  search_volume: 0,
  cpc: null,
  competition: 0,
  keyword_difficulty: null,
  intent: "unknown",
  trend: [],
};
const run: ResearchRun = {
  run_id: "run-one",
  project_id: "p1",
  status: "partial",
  observed_at: "2026-10-14T12:00:00Z",
  location_code: 2840,
  language_code: "en",
  results: [
    {
      seed: "seo",
      ok: true,
      source: "auto",
      used_fallback: false,
      rows: [row],
    },
    {
      seed: "failed",
      ok: false,
      source: "auto",
      used_fallback: false,
      rows: [],
      error: "Seed provider failure",
    },
  ],
  cost_usd: null,
  cached: false,
};
const originalComputedStyle = window.getComputedStyle;
beforeEach(() => {
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    originalComputedStyle(element),
  );
  vi.clearAllMocks();
  vi.mocked(seoApi.projects).mockResolvedValue([
    {
      project_id: "p1",
      name: "Site",
      domain: "example.com",
      agent_id: "agent-one",
      location_code: 2840,
      language_code: "en",
      created_at: "2026-10-14T12:00:00Z",
    },
  ]);
  vi.mocked(seoApi.provider).mockResolvedValue({ configured: true });
  vi.mocked(seoApi.runs).mockResolvedValue([]);
  vi.mocked(seoApi.keywords).mockResolvedValue({ rows: [], total_count: 0 });
  vi.mocked(seoApi.reports).mockResolvedValue([]);
  vi.mocked(seoApi.research).mockResolvedValue(run);
  vi.mocked(seoApi.saveKeywords).mockResolvedValue({
    saved_count: 1,
    created_count: 1,
    updated_count: 0,
  });
  vi.mocked(octopThreadsApi.create).mockResolvedValue({
    thread_id: "thread-one",
    session_key: "web:test",
  });
});
function setup(path = "/workbench/seo-workbench.main") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <SeoWorkbench />
      <Location />
    </MemoryRouter>,
  );
}
async function startResearch() {
  await screen.findByLabelText("seo.seeds");
  fireEvent.change(screen.getByLabelText("seo.seeds"), {
    target: { value: "seo" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "seo.confirmPaid" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "seo.runResearch" }));
  });
}
describe("SEO native workflow", () => {
  it("only queries on explicit consent and submission; saves selected evidence then opens report chat", async () => {
    setup();
    await screen.findByLabelText("seo.seeds");
    expect(
      screen.getByRole("button", { name: "seo.runResearch" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("tab", { name: "seo.saved" }));
    fireEvent.click(screen.getByRole("tab", { name: "seo.reports" }));
    fireEvent.click(screen.getByRole("tab", { name: "seo.research" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "seo.clickstream" }));
    expect(seoApi.research).not.toHaveBeenCalled();
    await startResearch();
    expect(await screen.findByText("zero volume")).toBeVisible();
    expect(screen.getByText("failed: Seed provider failure")).toBeVisible();
    const resultRow = screen.getByText("zero volume").closest("tr")!;
    expect(within(resultRow).getAllByText("0")).toHaveLength(2);
    expect(within(resultRow).getAllByText("seo.missing")).toHaveLength(3);
    fireEvent.click(within(resultRow).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "seo.saveKeywords" }));
    await waitFor(() =>
      expect(seoApi.saveKeywords).toHaveBeenCalledWith("p1", {
        run_id: "run-one",
        keywords: ["zero volume"],
        tags: [],
        tag_mode: "append",
      }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "seo.generateReport" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "seo.generateReport" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/chat/agent-one/thread-one",
      ),
    );
    expect(octopThreadsApi.create).toHaveBeenCalledWith("agent-one");
    expect(screen.getByTestId("location")).toHaveTextContent(
      "Report for p1/run-one",
    );
    expect(seoApi.research).toHaveBeenCalledTimes(1);
    expect(vi.mocked(seoApi.research).mock.calls[0][1].group_keywords).toBe(
      false,
    );
  });
  it("preserves idempotency after an uncertain network failure and exposes recovery", async () => {
    vi.mocked(seoApi.research).mockRejectedValueOnce(
      new Error("Network unavailable"),
    );
    setup();
    await startResearch();
    expect(await screen.findByText("seo.recoverHelp")).toBeVisible();
    expect(screen.queryByText("zero volume")).not.toBeInTheDocument();
    const key = vi.mocked(seoApi.research).mock.calls[0][1].idempotency_key;
    fireEvent.click(screen.getByRole("button", { name: "seo.runResearch" }));
    await screen.findByText("zero volume");
    expect(vi.mocked(seoApi.research).mock.calls[1][1].idempotency_key).toBe(
      key,
    );
  });
  it("opens the requested project and reports view from a report deep link without research", async () => {
    vi.mocked(seoApi.projects).mockResolvedValue([
      {
        project_id: "p1",
        name: "Site",
        domain: "one.test",
        agent_id: "a1",
        location_code: 2840,
        language_code: "en",
        created_at: "2026-10-14T12:00:00Z",
      },
      {
        project_id: "p2",
        name: "Second",
        domain: "two.test",
        agent_id: "a2",
        location_code: 2840,
        language_code: "en",
        created_at: "2026-10-14T12:00:00Z",
      },
    ]);
    setup("/workbench/seo-workbench.main?project=p2&view=reports");
    await waitFor(() => expect(seoApi.reports).toHaveBeenCalledWith("p2"));
    expect(screen.getByRole("tab", { name: "seo.reports" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(seoApi.research).not.toHaveBeenCalled();
  });
  it("clears selection and preserves the viewed run after returning to the workbench", async () => {
    vi.mocked(seoApi.runs).mockResolvedValue([run]);
    const view = render(
      <MemoryRouter>
        <SeoWorkbench isVisible />
      </MemoryRouter>,
    );
    const keyword = await screen.findByText("zero volume");
    fireEvent.click(within(keyword.closest("tr")!).getByRole("checkbox"));
    expect(
      screen.getByRole("button", { name: "seo.saveKeywords" }),
    ).toBeEnabled();
    view.rerender(
      <MemoryRouter>
        <SeoWorkbench isVisible={false} />
      </MemoryRouter>,
    );
    vi.mocked(seoApi.runs).mockResolvedValue([
      {
        ...run,
        run_id: "new-run",
        results: [
          {
            seed: "new",
            source: "auto",
            ok: true,
            used_fallback: false,
            rows: [{ ...row, keyword: "new result" }],
          },
        ],
      },
      run,
    ]);
    view.rerender(
      <MemoryRouter>
        <SeoWorkbench isVisible />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "seo.saveKeywords" }),
      ).toBeDisabled(),
    );
    expect(screen.getByText("zero volume")).toBeVisible();
    expect(screen.queryByText("new result")).not.toBeInTheDocument();
    expect(seoApi.saveKeywords).not.toHaveBeenCalled();
    expect(seoApi.research).not.toHaveBeenCalled();
  });
  it("does not replace missing credentials with simulated results", async () => {
    vi.mocked(seoApi.provider).mockResolvedValue({ configured: false });
    setup();
    await screen.findByLabelText("seo.seeds");
    fireEvent.click(screen.getByRole("checkbox", { name: "seo.confirmPaid" }));
    expect(
      screen.getByRole("button", { name: "seo.runResearch" }),
    ).toBeDisabled();
    expect(seoApi.research).not.toHaveBeenCalled();
  });
});
describe("static private report preview", () => {
  it("strips active/navigation content and prepends network-blocking CSP", () => {
    const result = staticReportHtml(
      '<html><head><meta http-equiv="refresh" content="0; url=https://evil.test"></head><body onload="alert(1)"><h1>Report</h1><a href="https://evil.test" ping="https://evil.test">Link</a><script>alert(1)</script><form action="/api"><input></form><img src="https://evil.test"><style>body{background:url(https://evil.test)}</style></body></html>',
    );
    expect(result).toContain("Report");
    expect(result).not.toMatch(
      /<script|<form|onload=|href=|src=|ping=|http-equiv="refresh"/i,
    );
    expect(result).toContain("default-src 'none'");
    expect(result.indexOf("Content-Security-Policy")).toBeLessThan(
      result.indexOf("<style>"),
    );
  });
});
