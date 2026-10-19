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
import {
  seoApi,
  type ProjectContext,
  type ResearchRun,
  type RunSummary,
  type SeoAuditDetail,
} from "../../../api/modules/seo";
import { octopThreadsApi } from "../../../api/modules/octopThreads";
import { staticReportHtml } from "./reportPreview";

const translate = vi.hoisted(
  () => (key: string, values?: Record<string, unknown>) =>
    key === "seo.reportPrompt"
      ? `Report for ${values?.projectId}/${values?.runId}`
      : key,
);
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock("../../../api/modules/seo", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/modules/seo")>()),
  seoApi: {
    projects: vi.fn(),
    provider: vi.fn(),
    createProject: vi.fn(),
    bindAgent: vi.fn(),
    configureProvider: vi.fn(),
    runs: vi.fn(),
    run: vi.fn(),
    research: vi.fn(),
    buySerp: vi.fn(),
    serps: vi.fn(),
    serp: vi.fn(),
    context: vi.fn(),
    updateContext: vi.fn(),
    keywords: vi.fn(),
    saveKeywords: vi.fn(),
    reports: vi.fn(),
    report: vi.fn(),
    startAudit: vi.fn(),
    audits: vi.fn(),
    audit: vi.fn(),
    auditIssues: vi.fn(),
    auditPages: vi.fn(),
    cancelAudit: vi.fn(),
    deleteAudit: vi.fn(),
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
function summary(full: ResearchRun): RunSummary {
  return {
    ...full,
    results: full.results.map(({ rows, ...rest }) => ({
      ...rest,
      row_count: rows.length,
    })),
  };
}
const project = {
  project_id: "p1",
  name: "Site",
  domain: "example.com",
  agent_id: "agent-one",
  location_code: 2840,
  language_code: "en",
  created_at: "2026-10-14T12:00:00Z",
};
const originalComputedStyle = window.getComputedStyle;
beforeEach(() => {
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) =>
    originalComputedStyle(element),
  );
  vi.clearAllMocks();
  window.sessionStorage.clear();
  vi.mocked(seoApi.projects).mockResolvedValue([project]);
  vi.mocked(seoApi.provider).mockResolvedValue({ configured: true });
  vi.mocked(seoApi.runs).mockResolvedValue([]);
  vi.mocked(seoApi.keywords).mockResolvedValue({ rows: [], total_count: 0 });
  vi.mocked(seoApi.reports).mockResolvedValue([]);
  vi.mocked(seoApi.audits).mockResolvedValue([]);
  vi.mocked(seoApi.research).mockResolvedValue(run);
  vi.mocked(seoApi.run).mockImplementation(async (_, runId) => {
    if (runId !== run.run_id) throw new Error(`unexpected run ${runId}`);
    return run;
  });
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
    vi.mocked(seoApi.runs).mockResolvedValue([summary(run)]);
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
      summary({
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
      }),
      summary(run),
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
  it("opens one tab per seed, loads only the open run and restores tabs for the session", async () => {
    vi.mocked(seoApi.runs).mockResolvedValue([summary(run)]);
    const view = render(
      <MemoryRouter>
        <SeoWorkbench />
      </MemoryRouter>,
    );
    expect(await screen.findByText("zero volume")).toBeVisible();
    expect(screen.getByRole("tab", { name: /^seo\s/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const failedTab = screen.getByRole("tab", { name: /^failed\s/ });
    fireEvent.click(failedTab);
    await waitFor(() =>
      expect(screen.queryByText("zero volume")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("seo.emptyResults")).toBeVisible();
    expect(seoApi.run).toHaveBeenCalledTimes(1);
    // Closing the active last tab selects its left neighbour.
    fireEvent.click(
      failedTab
        .closest(".ant-tabs-tab")!
        .querySelector(".ant-tabs-tab-remove")!,
    );
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /^seo\s/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(
      screen.queryByRole("tab", { name: /^failed\s/ }),
    ).not.toBeInTheDocument();
    view.unmount();
    // A newer run does not replace the tabs restored from this session.
    vi.mocked(seoApi.runs).mockResolvedValue([
      summary({ ...run, run_id: "new-run" }),
      summary(run),
    ]);
    render(
      <MemoryRouter>
        <SeoWorkbench />
      </MemoryRouter>,
    );
    expect(await screen.findByText("zero volume")).toBeVisible();
    expect(
      screen.getAllByRole("tab", { name: /^(seo|failed)\s/ }),
    ).toHaveLength(1);
    expect(seoApi.run).not.toHaveBeenCalledWith("p1", "new-run");
    expect(seoApi.research).not.toHaveBeenCalled();
  });
  it("reads saved SERP snapshots on open and buys only on a confirmed click", async () => {
    vi.mocked(seoApi.runs).mockResolvedValue([summary(run)]);
    vi.mocked(seoApi.serps).mockResolvedValue([]);
    vi.mocked(seoApi.buySerp)
      .mockRejectedValueOnce(new Error("Network unavailable"))
      .mockResolvedValueOnce({
        serp_id: "serp-one",
        keyword: "zero volume",
        device: "desktop",
        depth: 20,
        requested_depth: 20,
        status: "completed",
        observed_at: "2026-10-14T12:00:00Z",
        item_count: 2,
        cost_usd: 0.002,
        cached: false,
        items: [
          {
            type: "organic",
            rank: 1,
            organic_rank: 1,
            title: "Safe result",
            url: "https://safe.test/a",
            domain: "safe.test",
            description: null,
          },
          {
            type: "organic",
            rank: 2,
            organic_rank: 2,
            title: "Unsafe result",
            url: "javascript:alert(1)",
            domain: "evil.test",
            description: null,
          },
        ],
      });
    setup();
    const keyword = await screen.findByText("zero volume");
    fireEvent.click(
      within(keyword.closest("tr")!).getByRole("button", {
        name: "seo.viewSerp",
      }),
    );
    expect(await screen.findByText("seo.serpNotBought")).toBeVisible();
    expect(seoApi.serps).toHaveBeenCalledWith("p1", "zero volume");
    expect(seoApi.buySerp).not.toHaveBeenCalled();
    async function buyTop20() {
      fireEvent.click(screen.getByRole("button", { name: "seo.serpBuyTop20" }));
      const confirm = await screen.findAllByRole("button", {
        name: "seo.serpConfirmBuy",
      });
      await act(async () => {
        fireEvent.click(confirm[confirm.length - 1]);
      });
    }
    await buyTop20();
    expect(await screen.findByText("Network unavailable")).toBeVisible();
    await buyTop20();
    expect(await screen.findByText("Safe result")).toHaveAttribute(
      "href",
      "https://safe.test/a",
    );
    expect(screen.getByText("Unsafe result").closest("a")).toBeNull();
    const [first, second] = vi.mocked(seoApi.buySerp).mock.calls;
    expect(first).toEqual([
      "p1",
      {
        keyword: "zero volume",
        device: "desktop",
        depth: 20,
        idempotency_key: expect.any(String),
        confirm_paid: true,
      },
    ]);
    expect(second[1].idempotency_key).toBe(first[1].idempotency_key);
  });
  it("loads project background only when opened and saves each change as one batch", async () => {
    const context: ProjectContext = {
      sections: [],
      missing_sections: [
        "business_overview",
        "current_goal",
        "positioning",
        "writing_preferences",
      ],
      custom_sections: [],
      competitors: [],
      key_pages: [],
      research_log: [
        {
          log_id: "log-one",
          entry_date: "2026-10-14",
          summary: "Wrote the first report",
          created_by: "agent",
          created_at: "2026-10-14T12:00:00Z",
        },
      ],
      research_log_limit: 20,
    };
    vi.mocked(seoApi.context).mockResolvedValue(context);
    vi.mocked(seoApi.updateContext).mockResolvedValue(context);
    setup();
    await screen.findByLabelText("seo.seeds");
    expect(seoApi.context).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "seo.context" }));
    fireEvent.change(
      await screen.findByLabelText("seo.section_business_overview"),
      { target: { value: "Short drama apps" } },
    );
    const enabled = screen
      .getAllByRole("button", { name: "seo.contextSave" })
      .filter((button) => !button.hasAttribute("disabled"));
    expect(enabled).toHaveLength(1);
    fireEvent.click(enabled[0]);
    await waitFor(() =>
      expect(seoApi.updateContext).toHaveBeenCalledWith("p1", [
        { section: "business_overview", content: "Short drama apps" },
      ]),
    );
    fireEvent.change(screen.getByLabelText("seo.competitorDomain"), {
      target: { value: "rival.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "seo.addCompetitor" }));
    await waitFor(() =>
      expect(seoApi.updateContext).toHaveBeenLastCalledWith("p1", [
        { add_competitors: [{ domain: "rival.test" }] },
      ]),
    );
    fireEvent.click(
      within(
        screen.getByText("Wrote the first report").closest("li")!,
      ).getByRole("button", { name: "seo.contextRemove" }),
    );
    await waitFor(() =>
      expect(seoApi.updateContext).toHaveBeenLastCalledWith("p1", [
        { remove_research_log: ["log-one"] },
      ]),
    );
  });
  it("keeps an unbound project readable and rebinds it to another expert", async () => {
    vi.mocked(seoApi.projects).mockResolvedValue([
      { ...project, agent_id: null },
    ]);
    vi.mocked(seoApi.runs).mockResolvedValue([summary(run)]);
    vi.mocked(seoApi.bindAgent).mockResolvedValue(project);
    setup();
    expect(await screen.findByText("zero volume")).toBeVisible();
    expect(screen.getByText("seo.unbound")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "seo.generateReport" }),
    ).toBeDisabled();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "seo.agent" }));
    fireEvent.click(await screen.findByTitle("My SEO"));
    fireEvent.click(screen.getByRole("button", { name: "seo.bindAgent" }));
    await waitFor(() =>
      expect(seoApi.bindAgent).toHaveBeenCalledWith("p1", "agent-one"),
    );
    await waitFor(() =>
      expect(screen.queryByText("seo.unbound")).not.toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: "seo.generateReport" }),
    ).toBeEnabled();
  });
});
const audit: SeoAuditDetail = {
  audit_id: "audit-one",
  project_id: "p1",
  requested_url: "https://example.com/",
  start_url: "https://example.com/",
  max_pages: 50,
  status: "completed",
  phase: "finalize",
  pages_crawled: 50,
  pages_total: 50,
  discovered_count: 80,
  sitemap_count: 60,
  crawl_complete: false,
  rate_limited: false,
  error_code: null,
  error_detail: null,
  cancel_requested: false,
  created_at: "2026-10-16T08:00:00Z",
  started_at: "2026-10-16T08:00:01Z",
  completed_at: "2026-10-16T08:02:00Z",
  issue_counts: { critical: 1, warning: 0, info: 0 },
};
describe("site audit", () => {
  beforeEach(() => {
    vi.mocked(seoApi.startAudit).mockResolvedValue({
      ...audit,
      status: "queued",
      phase: "discovery",
    });
    vi.mocked(seoApi.audit).mockResolvedValue(audit);
    vi.mocked(seoApi.auditIssues).mockResolvedValue({
      summary: [
        {
          issue_type: "broken-internal-link",
          severity: "critical",
          count: 1,
          title: "Broken internal link",
          explanation: "Links to an error page.",
          how_to_fix: "Point the link at a live URL.",
        },
      ],
      issues: [
        {
          issue_type: "broken-internal-link",
          severity: "critical",
          page_url: "https://example.com/a",
          details: {
            target_url: "https://example.com/gone",
            target_status: 404,
          },
        },
      ],
    });
    vi.mocked(seoApi.auditPages).mockResolvedValue({ pages: [] });
  });
  it("starts an audit for the project domain and explains partial results", async () => {
    setup("/workbench/seo-workbench.main?project=p1&view=audit");
    const start = await screen.findByRole("button", { name: "seo.auditStart" });
    expect(screen.getByLabelText("seo.auditStartUrl")).toHaveValue(
      "https://example.com/",
    );
    await act(async () => {
      fireEvent.click(start);
    });
    expect(seoApi.startAudit).toHaveBeenCalledWith("p1", {
      start_url: "https://example.com/",
      max_pages: 50,
      idempotency_key: expect.any(String),
    });
    expect(await screen.findByText("Broken internal link")).toBeVisible();
    expect(screen.getByText("seo.auditPageLimit")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /expand row/i }));
    expect(
      await screen.findByText("Point the link at a live URL."),
    ).toBeVisible();
    expect(
      screen.getByText(
        "target_url: https://example.com/gone · target_status: 404",
      ),
    ).toBeVisible();
  });
  it("blocks a second audit while one runs and cancels it", async () => {
    const running = {
      ...audit,
      status: "running" as const,
      phase: "crawl" as const,
    };
    vi.mocked(seoApi.audits).mockResolvedValue([running]);
    vi.mocked(seoApi.audit).mockResolvedValue(running);
    vi.mocked(seoApi.cancelAudit).mockResolvedValue({
      ...running,
      cancel_requested: true,
    });
    setup("/workbench/seo-workbench.main?project=p1&view=audit");
    expect(await screen.findByText("seo.auditBusy")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "seo.auditStart" }),
    ).toBeDisabled();
    fireEvent.click(
      await screen.findByRole("button", { name: "seo.auditCancel" }),
    );
    await waitFor(() =>
      expect(seoApi.cancelAudit).toHaveBeenCalledWith("p1", "audit-one"),
    );
    expect(await screen.findByText("seo.auditCancelling")).toBeVisible();
    expect(seoApi.startAudit).not.toHaveBeenCalled();
  });
  it("says the server is restarting while polls fail and clears it once a poll answers", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      const running = {
        ...audit,
        status: "running" as const,
        phase: "crawl" as const,
        pages_crawled: 54,
      };
      vi.mocked(seoApi.audits).mockResolvedValue([running]);
      vi.mocked(seoApi.audit)
        .mockResolvedValueOnce(running)
        .mockRejectedValueOnce(
          new Error(
            'Request failed: 502 Bad Gateway - {"status":"error","code":502,"message":"Application failed to respond","request_id":"r1"}',
          ),
        )
        .mockResolvedValue({ ...running, pages_crawled: 60 });
      // The first read finds the audit running, which starts polling with an
      // immediate second read; that one hits the restart.
      setup("/workbench/seo-workbench.main?project=p1&view=audit");
      expect(
        await screen.findByText("seo.auditServerRestarting"),
      ).toBeVisible();
      expect(seoApi.audit).toHaveBeenCalledTimes(2);
      expect(
        screen.queryByText("Application failed to respond"),
      ).not.toBeInTheDocument();
      await act(async () => {
        vi.advanceTimersByTime(3000);
      });
      await waitFor(() =>
        expect(
          screen.queryByText("seo.auditServerRestarting"),
        ).not.toBeInTheDocument(),
      );
      expect(seoApi.audit).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
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
