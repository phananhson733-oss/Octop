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
  type AiRun,
  type AiVisibility,
  type ProjectContext,
  type RankResults,
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
    buyLookup: vi.fn(),
    lookups: vi.fn(),
    lookup: vi.fn(),
    buyPromptLookup: vi.fn(),
    buyBrandLookup: vi.fn(),
    aiLookups: vi.fn(),
    aiLookup: vi.fn(),
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
    google: vi.fn(),
    connectGoogle: vi.fn(),
    disconnectGoogle: vi.fn(),
    gsc: vi.fn(),
    gscSites: vi.fn(),
    bindGsc: vi.fn(),
    unbindGsc: vi.fn(),
    gscReport: vi.fn(),
    gscTable: vi.fn(),
    gscInspect: vi.fn(),
    ga4: vi.fn(),
    ga4Properties: vi.fn(),
    bindGa4: vi.fn(),
    unbindGa4: vi.fn(),
    ga4Overview: vi.fn(),
    rankTrackers: vi.fn(),
    createRankTracker: vi.fn(),
    rankResults: vi.fn(),
    updateRankTracker: vi.fn(),
    deleteRankTracker: vi.fn(),
    addRankKeywords: vi.fn(),
    removeRankKeyword: vi.fn(),
    rankHistory: vi.fn(),
    startRankCheck: vi.fn(),
    aiVisibility: vi.fn(),
    setAiEngines: vi.fn(),
    addAiPrompts: vi.fn(),
    removeAiPrompt: vi.fn(),
    aiAnswer: vi.fn(),
    startAiCheck: vi.fn(),
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
const admin = { id: 1, role: "admin" };
const currentUser = vi.hoisted(() => ({
  value: { id: 1, role: "admin" } as { id: number; role: string },
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => currentUser.value,
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
  currentUser.value = admin;
  vi.mocked(seoApi.projects).mockResolvedValue([project]);
  vi.mocked(seoApi.provider).mockResolvedValue({ configured: true });
  vi.mocked(seoApi.runs).mockResolvedValue([]);
  vi.mocked(seoApi.keywords).mockResolvedValue({ rows: [], total_count: 0 });
  vi.mocked(seoApi.reports).mockResolvedValue([]);
  vi.mocked(seoApi.audits).mockResolvedValue([]);
  vi.mocked(seoApi.aiLookups).mockResolvedValue([]);
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
  it("lets an account with SEO access research the admin's project without its setup controls", async () => {
    currentUser.value = { id: 2, role: "01ROLE" };
    setup();
    await startResearch();
    expect(await screen.findByText("zero volume")).toBeVisible();
    expect(seoApi.research).toHaveBeenCalled();
    for (const name of [
      "seo.createProject",
      "seo.provider",
      "seo.generateReport",
    ]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });
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
  it("opens the linked report's preview from a chat report link", async () => {
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
    vi.mocked(seoApi.report).mockResolvedValue({
      report_id: "r1",
      project_id: "p2",
      run_id: "run-1",
      title: "Linked report",
      summary: "",
      skill: "keyword-research",
      created_at: "2026-10-19T07:00:00Z",
      content_hash: "hash",
      size_bytes: 10,
      html: "<p>Body</p>",
    });
    setup("/workbench/seo-workbench.main?project=p2&view=reports&report=r1");
    expect(await screen.findByText("Linked report")).toBeInTheDocument();
    // Only the linked project's report is fetched, not one under the first project.
    expect(seoApi.report).toHaveBeenCalledTimes(1);
    expect(seoApi.report).toHaveBeenCalledWith("p2", "r1");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() =>
      expect(screen.queryByText("Linked report")).not.toBeInTheDocument(),
    );
    expect(seoApi.report).toHaveBeenCalledTimes(1);
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
describe("search performance", () => {
  const account = {
    grant_id: "grant-one",
    email: "owner@example.com",
    search_console: true,
    analytics: true,
    updated_at: 1,
  };
  const connection = {
    project_id: "p1",
    grant_id: "grant-one",
    site_url: "sc-domain:example.com",
    permission_level: "siteOwner",
    connected_at: 1,
    email: "owner@example.com",
  };
  const totals = { clicks: 10, impressions: 100, ctr: 0.1, position: 8 };
  beforeEach(() => {
    vi.mocked(seoApi.google).mockResolvedValue({
      configured: true,
      redirect_uri: "https://octop.test/api/seo/google/callback",
      accounts: [account],
    });
    vi.mocked(seoApi.gsc).mockResolvedValue({ connection: null });
    vi.mocked(seoApi.gscSites).mockResolvedValue({
      accounts: [
        {
          ...account,
          requires_reconnect: false,
          api_disabled: false,
          sites: [
            {
              site_url: "sc-domain:example.com",
              permission_level: "siteOwner",
            },
            {
              site_url: "https://other.example/",
              permission_level: "siteUnverifiedUser",
            },
          ],
        },
      ],
    });
    vi.mocked(seoApi.bindGsc).mockResolvedValue(connection);
    vi.mocked(seoApi.gscReport).mockResolvedValue({
      site_url: "sc-domain:example.com",
      email: "owner@example.com",
      range: {
        start_date: "2026-09-18",
        end_date: "2026-10-16",
        prev_start_date: "2026-08-20",
        prev_end_date: "2026-09-17",
      },
      totals,
      prev_totals: { ...totals, clicks: 5, position: 9 },
      daily: [],
      striking_distance: [
        {
          query: "near query",
          page: "https://example.com/a",
          clicks: 1,
          impressions: 50,
          position: 7.4,
        },
      ],
      countries: [
        { key: "usa", clicks: 10, impressions: 100, ctr: 0.1, position: 8 },
      ],
    });
    vi.mocked(seoApi.gscTable).mockResolvedValue({
      dimension: "query",
      page: 1,
      page_size: 25,
      has_next_page: true,
      total_count: null,
      rows: [
        { key: "top query", clicks: 9, impressions: 90, ctr: 0.1, position: 2 },
      ],
    });
    vi.mocked(seoApi.ga4).mockResolvedValue({ connection: null });
    vi.mocked(seoApi.ga4Properties).mockResolvedValue({
      accounts: [
        {
          ...account,
          requires_reconnect: false,
          api_disabled: false,
          properties: [
            {
              property_id: "properties/123",
              display_name: "Example web",
              account_display_name: "Example account",
            },
          ],
        },
      ],
    });
  });
  it("leaves Google accounts to the admin for an account with SEO access", async () => {
    currentUser.value = { id: 2, role: "01ROLE" };
    setup("/workbench/seo-workbench.main?project=p1&view=search");
    expect(await screen.findByText("owner@example.com")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "seo.gscDisconnect" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "seo.gscConnectAnother" }),
    ).not.toBeInTheDocument();
  });

  it("binds a verified property, then reads totals, queries and striking distance", async () => {
    setup("/workbench/seo-workbench.main?project=p1&view=search");
    const picker = await screen.findByRole("combobox", {
      name: "seo.gscProperty",
    });
    await waitFor(() => expect(seoApi.gscSites).toHaveBeenCalledWith("p1"));
    fireEvent.mouseDown(picker);
    const unverified = await screen.findByText(
      "https://other.example/ (seo.gscUnverified)",
    );
    expect(
      unverified.closest(".ant-select-item-option-disabled"),
    ).not.toBeNull();
    fireEvent.click(screen.getByTitle("sc-domain:example.com"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "seo.gscBind" }));
    });
    expect(seoApi.bindGsc).toHaveBeenCalledWith("p1", {
      grant_id: "grant-one",
      site_url: "sc-domain:example.com",
    });
    expect(await screen.findByText("top query")).toBeVisible();
    expect(seoApi.gscReport).toHaveBeenCalledWith("p1", {
      date_range: "last_28_days",
    });
    expect(seoApi.gscTable).toHaveBeenCalledWith("p1", {
      date_range: "last_28_days",
      dimension: "query",
      page: 1,
      page_size: 25,
    });
    // Clicks doubled against the previous period; position fell by one.
    expect(screen.getByText("+100%")).toBeVisible();
    expect(screen.getByText("-1.0")).toBeVisible();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "seo.gscNext" }));
    });
    expect(seoApi.gscTable).toHaveBeenLastCalledWith(
      "p1",
      expect.objectContaining({ page: 2 }),
    );
    fireEvent.click(screen.getByRole("tab", { name: "seo.gscStriking" }));
    expect(await screen.findByText("near query")).toBeVisible();
  });
  it("binds a GA4 property and compares organic traffic with the previous period", async () => {
    const metrics = (sessions: number, keyEvents: number) => ({
      sessions,
      activeUsers: 80,
      engagedSessions: 70,
      engagementRate: 0.5,
      keyEvents,
      transactions: 0,
      purchaseRevenue: 0,
    });
    vi.mocked(seoApi.bindGa4).mockResolvedValue({
      project_id: "p1",
      grant_id: "grant-one",
      property_id: "properties/123",
      display_name: "Example web",
      time_zone: "America/New_York",
      currency_code: "USD",
      connected_at: 1,
      email: "owner@example.com",
    });
    vi.mocked(seoApi.ga4Overview).mockResolvedValue({
      source: {
        property_id: "properties/123",
        display_name: "Example web",
        email: "owner@example.com",
      },
      request: {
        requested_date_range: null,
        resolved_date_range: {
          start_date: "2026-09-21",
          end_date: "2026-10-18",
        },
        previous_date_range: {
          start_date: "2026-08-24",
          end_date: "2026-09-20",
        },
        time_zone: "America/New_York",
        currency_code: "USD",
        channel: "organic_search",
        trend: "daily",
      },
      current: metrics(150, 2),
      previous: metrics(100, 8),
      comparison: {
        sessions: {
          current: 150,
          previous: 100,
          absolute_change: 50,
          percent_change: 0.5,
        },
        engagementRate: {
          current: 0.5,
          previous: 0.4,
          absolute_change: 0.1,
          percent_change: 0.25,
        },
        keyEvents: {
          current: 2,
          previous: 8,
          absolute_change: -6,
          percent_change: -0.75,
        },
      },
      trend: [{ ...metrics(150, 2), date: "20261018" }],
      diagnostics: [{ code: "key_events_sharp_decline" }],
      has_limited_data: false,
      warnings: [],
    });
    setup("/workbench/seo-workbench.main?project=p1&view=search");
    const picker = await screen.findByRole("combobox", {
      name: "seo.ga4Property",
    });
    await waitFor(() =>
      expect(seoApi.ga4Properties).toHaveBeenCalledWith("p1"),
    );
    fireEvent.mouseDown(picker);
    fireEvent.click(await screen.findByTitle("Example web · Example account"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "seo.ga4Bind" }));
    });
    expect(seoApi.bindGa4).toHaveBeenCalledWith("p1", {
      grant_id: "grant-one",
      property_id: "properties/123",
    });
    expect(await screen.findByText("+50%")).toBeVisible();
    expect(seoApi.ga4Overview).toHaveBeenCalledWith("p1", { trend: "daily" });
    expect(screen.getByText("+10.0 pp")).toBeVisible();
    expect(screen.getByText("-75%")).toBeVisible();
    expect(screen.getByText("seo.ga4KeyEventsDrop")).toBeVisible();
    fireEvent.click(screen.getByText("seo.ga4Weekly"));
    await waitFor(() =>
      expect(seoApi.ga4Overview).toHaveBeenLastCalledWith("p1", {
        trend: "weekly",
      }),
    );
  });
  it("shows a failed Google status load with a retry instead of spinning", async () => {
    vi.mocked(seoApi.google)
      .mockRejectedValueOnce(new Error("database restarting"))
      .mockResolvedValue({
        configured: true,
        redirect_uri: "https://octop.test/api/seo/google/callback",
        accounts: [],
      });
    setup("/workbench/seo-workbench.main?project=p1&view=search");
    expect(await screen.findByText("database restarting")).toBeVisible();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "seo.gscRetry" }));
    });
    expect(
      await screen.findByRole("button", { name: "seo.gscConnect" }),
    ).toBeVisible();
    expect(screen.queryByText("database restarting")).toBeNull();
  });
  it("keeps the Analytics card and its error when the binding fails to load", async () => {
    vi.mocked(seoApi.ga4).mockRejectedValue(new Error("ga4 unavailable"));
    setup("/workbench/seo-workbench.main?project=p1&view=search");
    expect(await screen.findByText("ga4 unavailable")).toBeVisible();
    expect(screen.getByText("seo.ga4Title")).toBeVisible();
  });
  it("starts Google consent with the page to come back to", async () => {
    vi.mocked(seoApi.google).mockResolvedValue({
      configured: true,
      redirect_uri: "https://octop.test/api/seo/google/callback",
      accounts: [],
    });
    vi.mocked(seoApi.connectGoogle).mockResolvedValue({
      authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?state=s",
    });
    const original = window.location;
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...original, assign },
    });
    try {
      setup("/workbench/seo-workbench.main?project=p1&view=search");
      const connect = await screen.findByRole("button", {
        name: "seo.gscConnect",
      });
      expect(screen.getByText("seo.gscConnectFirst")).toBeVisible();
      await act(async () => {
        fireEvent.click(connect);
      });
      expect(seoApi.connectGoogle).toHaveBeenCalledWith(
        "/workbench/seo-workbench.main?project=p1&view=search",
      );
      expect(assign).toHaveBeenCalledWith(
        "https://accounts.google.com/o/oauth2/v2/auth?state=s",
      );
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: original,
      });
    }
  });
  it("explains the callback outcome and shows the redirect URI when unconfigured", async () => {
    vi.mocked(seoApi.google).mockResolvedValue({
      configured: false,
      redirect_uri: "https://octop.test/api/seo/google/callback",
      accounts: [],
    });
    setup(
      "/workbench/seo-workbench.main?project=p1&view=search&seo_google_error=scope_not_granted",
    );
    expect(
      await screen.findByText("seo.gscCallback_scope_not_granted"),
    ).toBeVisible();
    expect(screen.getByText("seo.gscNotConfigured")).toBeVisible();
    expect(screen.getByText("seo.gscRedirectHelp")).toBeVisible();
    expect(screen.queryByRole("button", { name: "seo.gscConnect" })).toBeNull();
  });
});
describe("competitors", () => {
  const emptyContext: ProjectContext = {
    sections: [],
    missing_sections: [],
    custom_sections: [],
    competitors: [],
    key_pages: [],
    research_log: [],
  };
  const lookupBase = {
    status: "completed" as const,
    observed_at: "2026-10-19T08:00:00Z",
    cost_usd: 0.03,
    cached: false,
    total_count: null,
    overview: null,
  };
  async function confirmBuy(name: string) {
    fireEvent.click(screen.getByRole("button", { name }));
    const confirm = await screen.findAllByRole("button", {
      name: "seo.serpConfirmBuy",
    });
    await act(async () => {
      fireEvent.click(confirm[confirm.length - 1]);
    });
  }
  it("reads saved lookups on open and buys a domain or competitor set only on a confirmed click", async () => {
    vi.mocked(seoApi.context).mockResolvedValue(emptyContext);
    vi.mocked(seoApi.lookups).mockResolvedValue([]);
    vi.mocked(seoApi.keywords).mockResolvedValue({
      rows: [
        { ...row, keyword: "small", search_volume: 10, tags: [] },
        { ...row, keyword: "big", search_volume: 900, tags: [] },
      ] as never,
      total_count: 2,
    });
    vi.mocked(seoApi.buyLookup).mockImplementation(async (_, data) =>
      data.kind === "domain"
        ? {
            ...lookupBase,
            lookup_id: "l-domain",
            kind: "domain",
            domain: "rival.com",
            keywords: null,
            item_count: 2,
            total_count: 4321,
            overview: {
              organic_traffic: 5000.4,
              organic_keywords: 4321,
              pos_1: 2,
              pos_2_3: 3,
              pos_4_10: 5,
            },
            items: [
              {
                keyword: "rival guide",
                rank: 3,
                organic_rank: 2,
                url: "https://rival.com/guide",
                etv: 80,
                search_volume: 1200,
                cpc: null,
                keyword_difficulty: 20,
                intent: "informational",
              },
              {
                keyword: "unsafe page",
                rank: 9,
                organic_rank: 8,
                url: "javascript:alert(1)",
                etv: 1,
                search_volume: 10,
                cpc: null,
                keyword_difficulty: null,
                intent: "unknown",
              },
            ],
          }
        : {
            ...lookupBase,
            lookup_id: "l-rivals",
            kind: "competitors",
            domain: null,
            keywords: ["big", "small"],
            item_count: 2,
            items: [
              {
                domain: "www.example.com",
                visibility: 0.4,
                avg_position: 4,
                median_position: 4,
                etv: 30,
                keywords_count: 1,
                keywords_positions: { big: [4] },
              },
              {
                domain: "rival.com",
                visibility: 0.9,
                avg_position: 1.5,
                median_position: 1,
                etv: 300,
                keywords_count: 2,
                keywords_positions: { big: [1], small: [2] },
              },
            ],
          },
    );
    vi.mocked(seoApi.updateContext).mockResolvedValue({
      ...emptyContext,
      competitors: [
        {
          domain: "rival.com",
          name: null,
          notes: null,
          updated_at: "2026-10-19T08:00:00Z",
          updated_by: "user",
        },
      ],
    });
    setup("/workbench/seo-workbench.main?project=p1&view=competitors");
    await waitFor(() =>
      expect(screen.getAllByText("seo.lookupNotBought")).toHaveLength(4),
    );
    expect(seoApi.lookups).toHaveBeenCalledWith("p1", "domain");
    expect(seoApi.lookups).toHaveBeenCalledWith("p1", "pages");
    expect(seoApi.lookups).toHaveBeenCalledWith("p1", "backlinks");
    expect(seoApi.lookups).toHaveBeenCalledWith("p1", "competitors");
    expect(seoApi.buyLookup).not.toHaveBeenCalled();

    const domainCard = screen
      .getByText("seo.lookupDomainTitle")
      .closest(".ant-card") as HTMLElement;
    const domainBox = within(domainCard).getByRole("combobox", {
      name: "seo.lookupDomain",
    });
    fireEvent.change(domainBox, {
      target: { value: "https://www.Rival.com/blog" },
    });
    await confirmBuy("seo.lookupDomainBuy");
    expect(await screen.findByText("rival guide")).toBeVisible();
    expect(seoApi.buyLookup).toHaveBeenCalledWith("p1", {
      kind: "domain",
      domain: "rival.com",
      idempotency_key: expect.any(String),
      confirm_paid: true,
    });
    expect(screen.getByText("5,000")).toBeVisible();
    const top10 = within(domainCard)
      .getByText("seo.lookupTop10")
      .closest(".ant-card");
    expect(within(top10 as HTMLElement).getByText("10")).toBeVisible();
    expect(screen.getByText("/guide")).toHaveAttribute(
      "href",
      "https://rival.com/guide",
    );
    expect(
      within(screen.getByText("unsafe page").closest("tr")!).queryByRole(
        "link",
      ),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "seo.lookupUseSaved" }));
    await waitFor(() =>
      expect(screen.getByLabelText("seo.lookupKeywords")).toHaveValue(
        "big\nsmall",
      ),
    );
    await confirmBuy("seo.lookupCompetitorsBuy");
    expect(seoApi.buyLookup).toHaveBeenLastCalledWith("p1", {
      kind: "competitors",
      keywords: ["big", "small"],
      idempotency_key: expect.any(String),
      confirm_paid: true,
    });
    const own = (await screen.findByText("www.example.com")).closest("tr")!;
    expect(within(own).getByText("seo.lookupOwn")).toBeVisible();
    expect(within(own).queryByRole("button")).toBeNull();
    const rival = screen
      .getByText("rival.com", { selector: "span" })
      .closest("tr")!;
    await act(async () => {
      fireEvent.click(
        within(rival).getByRole("button", { name: "seo.lookupAddCompetitor" }),
      );
    });
    expect(seoApi.updateContext).toHaveBeenCalledWith("p1", [
      { add_competitors: [{ domain: "rival.com" }] },
    ]);
    expect(await within(rival).findByText("seo.lookupSaved")).toBeVisible();
    expect(within(rival).queryByRole("button")).toBeNull();
  });
  it("buys a domain's top pages on a confirmed click and sums its top-3 and top-10 counts", async () => {
    vi.mocked(seoApi.context).mockResolvedValue(emptyContext);
    vi.mocked(seoApi.lookups).mockResolvedValue([]);
    vi.mocked(seoApi.buyLookup).mockResolvedValue({
      ...lookupBase,
      lookup_id: "l-pages",
      kind: "pages",
      domain: "rival.com",
      keywords: null,
      item_count: 2,
      total_count: 640,
      items: [
        {
          url: "https://rival.com/blog/best",
          etv: 812.4,
          keywords: 140,
          pos_1: 4,
          pos_2_3: 9,
          pos_4_10: 30,
        },
        {
          url: "javascript:alert(1)",
          etv: 3,
          keywords: null,
          pos_1: null,
          pos_2_3: 1,
          pos_4_10: 2,
        },
      ],
    });
    setup("/workbench/seo-workbench.main?project=p1&view=competitors");
    const card = (await screen.findByText("seo.lookupPagesTitle")).closest(
      ".ant-card",
    ) as HTMLElement;
    fireEvent.change(
      within(card).getByRole("combobox", { name: "seo.lookupDomain" }),
      { target: { value: "www.rival.com" } },
    );
    await confirmBuy("seo.lookupPagesBuy");
    expect(seoApi.buyLookup).toHaveBeenCalledWith("p1", {
      kind: "pages",
      domain: "rival.com",
      idempotency_key: expect.any(String),
      confirm_paid: true,
    });
    const best = (await within(card).findByText("/blog/best")).closest("tr")!;
    expect(within(best).getByRole("link")).toHaveAttribute(
      "href",
      "https://rival.com/blog/best",
    );
    expect(within(best).getByText("812")).toBeVisible();
    expect(within(best).getByText("13")).toBeVisible();
    expect(within(best).getByText("43")).toBeVisible();
    const unsafe = within(card).getByText("javascript:alert(1)").closest("tr")!;
    expect(within(unsafe).queryByRole("link")).toBeNull();
    // An unknown count leaves the sum unknown instead of undercounting.
    expect(within(unsafe).getAllByText("—")).toHaveLength(3);
    expect(within(card).getByText("seo.lookupPagesTopOf")).toBeVisible();
  });
  it("buys a domain's backlinks on a confirmed click and shows each section", async () => {
    vi.mocked(seoApi.context).mockResolvedValue(emptyContext);
    vi.mocked(seoApi.lookups).mockResolvedValue([]);
    vi.mocked(seoApi.buyLookup).mockResolvedValue({
      ...lookupBase,
      lookup_id: "l-links",
      kind: "backlinks",
      domain: "rival.com",
      keywords: null,
      item_count: 4,
      cost_usd: 0.13,
      overview: {
        rank: 41,
        backlinks: 12000,
        referring_pages: 9000,
        referring_domains: 640,
        broken_backlinks: 3,
        broken_pages: 1,
        backlinks_spam_score: 7,
        target_spam_score: 2,
        new_backlinks: 50,
        lost_backlinks: 20,
        new_referring_domains: 6,
        lost_referring_domains: 2,
      },
      items: [
        {
          section: "backlink",
          domain_from: "blog.test",
          url_from: "https://blog.test/review",
          url_to: "https://rival.com/drama",
          anchor: "best dramas",
          item_type: "anchor",
          dofollow: false,
          rank: 30,
          domain_from_rank: 55,
          spam_score: 4,
          first_seen: "2026-03-01 10:00:00 +00:00",
          last_seen: null,
          is_lost: false,
          is_broken: false,
        },
        {
          section: "referring_domain",
          domain: "news.test",
          backlinks: 12,
          rank: 60,
          spam_score: 1,
          first_seen: "2025-01-02 00:00:00 +00:00",
        },
        {
          section: "page",
          url: "javascript:alert(1)",
          backlinks: 99,
          referring_domains: 10,
          broken_backlinks: 0,
          rank: 20,
        },
        {
          section: "history",
          date: "2026-09-01",
          rank: 40,
          backlinks: 11000,
          referring_domains: 600,
          new_backlinks: 100,
          lost_backlinks: 50,
        },
      ],
    });
    setup("/workbench/seo-workbench.main?project=p1&view=competitors");
    const card = (await screen.findByText("seo.backlinksTitle")).closest(
      ".ant-card",
    ) as HTMLElement;
    fireEvent.change(
      within(card).getByRole("combobox", { name: "seo.lookupDomain" }),
      { target: { value: "https://rival.com/" } },
    );
    fireEvent.click(
      within(card).getByRole("button", { name: "seo.backlinksBuy" }),
    );
    expect(
      await screen.findByText("seo.backlinksPaidConfirm"),
    ).toBeInTheDocument();
    const confirm = await screen.findAllByRole("button", {
      name: "seo.serpConfirmBuy",
    });
    await act(async () => {
      fireEvent.click(confirm[confirm.length - 1]);
    });
    expect(seoApi.buyLookup).toHaveBeenCalledWith("p1", {
      kind: "backlinks",
      domain: "rival.com",
      idempotency_key: expect.any(String),
      confirm_paid: true,
    });
    expect(await within(card).findByText("12,000")).toBeVisible();
    expect(within(card).getByText("50 / 20")).toBeVisible();
    const link = within(card).getByText("best dramas").closest("tr")!;
    expect(within(link).getByText("nofollow")).toBeVisible();
    expect(within(link).getByText("/review")).toHaveAttribute(
      "href",
      "https://blog.test/review",
    );
    expect(within(link).getByText("2026-03-01")).toBeVisible();

    fireEvent.click(within(card).getByText("seo.backlinksDomainsTab"));
    expect(await within(card).findByText("news.test")).toBeVisible();
    fireEvent.click(within(card).getByText("seo.backlinksPagesTab"));
    const page = (await within(card).findByText("javascript:alert(1)")).closest(
      "tr",
    )!;
    expect(within(page).queryByRole("link")).toBeNull();
    expect(within(page).getByText("99")).toBeVisible();
  });
});
describe("rank tracking", () => {
  const cell = {
    position: null,
    url: null,
    serp_features: [],
    checked_at: null,
    previous_position: null,
    previous_checked_at: null,
  };
  const results: RankResults = {
    tracker: {
      tracker_id: "t1",
      project_id: "p1",
      domain: "example.com",
      location_code: 2840,
      language_code: "en",
      devices: "both",
      depth: 20,
      created_at: "2026-10-19T08:00:00Z",
      keyword_count: 2,
      last_checked_at: null,
      estimate_usd: 0.014,
    },
    run: null,
    compare: "7d",
    rows: [
      {
        keyword_id: "k1",
        keyword: "short drama",
        desktop: {
          ...cell,
          position: 3,
          url: "https://example.com/top",
          checked_at: "2026-10-19T09:00:00Z",
          previous_position: 5,
          previous_checked_at: "2026-10-10T09:00:00Z",
        },
        mobile: { ...cell, checked_at: "2026-10-19T09:00:00Z" },
      },
      { keyword_id: "k2", keyword: "reelshort", desktop: cell, mobile: cell },
    ],
  };
  it("creates a tracker for free and checks rankings only on a confirmed click at the shown maximum", async () => {
    vi.mocked(seoApi.rankTrackers).mockResolvedValue([]);
    vi.mocked(seoApi.createRankTracker).mockResolvedValue(results);
    vi.mocked(seoApi.rankResults).mockResolvedValue(results);
    const started = {
      run_id: "r1",
      status: "running",
      started_at: "2026-10-19T10:00:00Z",
      completed_at: null,
      devices: "both",
      depth: 20,
      keywords_total: 2,
      checks_total: 4,
      checks_done: 1,
      checks_failed: 0,
      cost_usd: 0.002,
      cost_known_usd: 0.002,
      estimate_usd: 0.014,
      error: null,
    } as const;
    vi.mocked(seoApi.startRankCheck).mockImplementation(async () => {
      // The server's tracker read shows the running check from now on.
      vi.mocked(seoApi.rankResults).mockResolvedValue({
        ...results,
        run: started,
      });
      return started;
    });
    setup("/workbench/seo-workbench.main?project=p1&view=rank");
    expect(await screen.findByText("seo.rankNone")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "seo.rankNew" }));
    fireEvent.change(screen.getByLabelText("seo.rankKeywords"), {
      target: { value: "Short Drama\nreelshort\nshort drama" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "seo.rankCreate" }));
    });
    expect(seoApi.createRankTracker).toHaveBeenCalledWith("p1", {
      devices: "both",
      depth: 20,
      keywords: ["short drama", "reelshort"],
    });
    const ranked = (await screen.findByText("short drama")).closest("tr")!;
    expect(within(ranked).getByText("3")).toBeVisible();
    expect(within(ranked).getByText("↑2")).toBeVisible();
    expect(within(ranked).getByText("seo.rankNotRanked")).toBeVisible();
    expect(within(ranked).getByRole("link")).toHaveAttribute(
      "href",
      "https://example.com/top",
    );
    expect(seoApi.startRankCheck).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "seo.rankCheck" }));
    const confirm = await screen.findAllByRole("button", {
      name: "seo.serpConfirmBuy",
    });
    await act(async () => {
      fireEvent.click(confirm[confirm.length - 1]);
    });
    expect(seoApi.startRankCheck).toHaveBeenCalledWith("p1", "t1", {
      idempotency_key: expect.any(String),
      confirm_paid: true,
      max_cost_usd: 0.014,
    });
    expect(await screen.findByText("seo.rankRunning")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "seo.rankCheck" }),
    ).toBeDisabled();
  });
});
describe("AI visibility", () => {
  const run: AiRun = {
    run_id: "r1",
    status: "completed",
    started_at: "2026-10-19T09:00:00Z",
    completed_at: "2026-10-19T09:01:00Z",
    engines: ["chatgpt"],
    brands: [
      { name: "Example", domain: "example.com", own: true },
      { name: "Rival", domain: "rival.com", own: false },
    ],
    prompts_total: 1,
    answers_total: 1,
    answers_done: 1,
    answers_failed: 0,
    cost_usd: 0.004,
    cost_known_usd: 0.004,
    estimate_usd: 0.004,
    error: null,
  };
  const visibility: AiVisibility = {
    settings: {
      engines: ["chatgpt"],
      location_code: 2840,
      language_code: "en",
      prompt_count: 1,
      estimate_usd: 0.004,
    },
    brands: run.brands,
    prompts: [
      {
        prompt_id: "q1",
        topic: "General",
        prompt: "best short drama apps",
        created_at: "2026-10-19T08:00:00Z",
        branded: false,
      },
    ],
    runs: [run],
    run,
    branded: "neutral",
    gap: false,
    coverage: { answered: 1, no_answer: 0, failed: 0 },
    summaries: [
      {
        ...run.brands[0],
        answers: 1,
        mentions: 1,
        citations: 0,
        avg_position: 2,
      },
      {
        ...run.brands[1],
        answers: 1,
        mentions: 1,
        citations: 1,
        avg_position: 1,
      },
    ],
    sources: [
      { domain: "rival.com", citations: 1, answers: 1, owner: "competitor" },
    ],
    rows: [
      {
        prompt_id: "q1",
        prompt: "best short drama apps",
        topic: "General",
        engine: "chatgpt",
        branded: false,
        status: "answered",
        brands: [
          {
            ...run.brands[0],
            mentioned: true,
            cited: false,
            first_mention: 30,
          },
          { ...run.brands[1], mentioned: true, cited: true, first_mention: 0 },
        ],
        citation_count: 1,
        error: null,
        cost_usd: 0.004,
        checked_at: "2026-10-19T09:01:00Z",
      },
    ],
  };
  it("shows answers as plain text and buys a check only on a confirmed click at the shown cost", async () => {
    vi.mocked(seoApi.aiVisibility).mockResolvedValue(visibility);
    vi.mocked(seoApi.addAiPrompts).mockResolvedValue({ added: 1 });
    const markdown = 'Rival <img src=x onerror="alert(1)"> then **Example**';
    vi.mocked(seoApi.aiAnswer).mockResolvedValue({
      ...visibility.rows[0],
      run_id: "r1",
      answer_markdown: markdown,
      citations: [
        {
          url: "https://rival.com/top",
          domain: "rival.com",
          title: "Rival top",
          position: 1,
          owner: "competitor",
        },
      ],
    });
    const started: AiRun = {
      ...run,
      run_id: "r2",
      status: "running",
      completed_at: null,
      answers_done: 0,
    };
    vi.mocked(seoApi.startAiCheck).mockImplementation(async () => {
      // The server's read shows the running check from now on.
      vi.mocked(seoApi.aiVisibility).mockResolvedValue({
        ...visibility,
        runs: [started, run],
        run: started,
        rows: [],
      });
      return started;
    });
    setup("/workbench/seo-workbench.main?project=p1&view=ai");
    const [, answered] = await screen.findAllByText("best short drama apps");
    const row = answered.closest("tr")!;
    expect(within(row).getByText("seo.aiMentioned")).toBeVisible();
    expect(within(row).getByText("Rival")).toBeVisible();
    expect(seoApi.aiVisibility).toHaveBeenCalledWith("p1", {
      run_id: undefined,
      branded: "neutral",
      gap: false,
    });

    fireEvent.click(within(row).getByRole("button", { name: "seo.aiOpen" }));
    expect(await screen.findByText(markdown)).toBeVisible();
    expect(seoApi.aiAnswer).toHaveBeenCalledWith("p1", "r1", "q1", "chatgpt");
    expect(document.querySelector("img[src='x']")).toBeNull();
    expect(screen.getByRole("link", { name: "Rival top" })).toHaveAttribute(
      "href",
      "https://rival.com/top",
    );

    fireEvent.change(screen.getByLabelText("seo.aiAddPrompts"), {
      target: { value: "free apps\nfree apps" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "seo.aiAddPrompts" }));
    });
    expect(seoApi.addAiPrompts).toHaveBeenCalledWith("p1", ["free apps"], "");
    expect(seoApi.startAiCheck).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "seo.aiCheck" }));
    const confirm = await screen.findAllByRole("button", {
      name: "seo.serpConfirmBuy",
    });
    await act(async () => {
      fireEvent.click(confirm[confirm.length - 1]);
    });
    expect(seoApi.startAiCheck).toHaveBeenCalledWith("p1", {
      idempotency_key: expect.any(String),
      confirm_paid: true,
      max_cost_usd: 0.004,
    });
    expect(await screen.findByText("seo.aiRunning")).toBeVisible();
    expect(screen.getByRole("button", { name: "seo.aiCheck" })).toBeDisabled();
  });
  it("asks a prompt of the chosen models on a confirmed click and shows each answer as plain text", async () => {
    vi.mocked(seoApi.aiVisibility).mockRejectedValue(new Error("not needed"));
    const answer = {
      model_name: null,
      status: "completed" as const,
      error: null,
      text: "",
      citations: [],
      fan_out_queries: [],
      output_tokens: null,
      web_search: null,
      cost_usd: 0,
    };
    vi.mocked(seoApi.buyPromptLookup).mockResolvedValue({
      lookup_id: "l-prompt",
      kind: "prompt",
      request: {
        prompt: "best free apps",
        models: ["chat_gpt", "claude", "gemini"],
        web_search: true,
        country: "US",
      },
      status: "completed",
      observed_at: "2026-10-23T08:00:00Z",
      item_count: 3,
      cost_usd: 0.03,
      cached: false,
      items: [
        {
          ...answer,
          model: "chat_gpt",
          model_name: "gpt-5.6-luna",
          text: "**Example** is great.<img src='x'>",
          citations: [
            {
              url: "https://example.com/top",
              domain: "example.com",
              title: "Example top",
            },
            { url: "javascript:alert(1)", domain: null, title: "Bad link" },
          ],
          fan_out_queries: ["free drama apps"],
          web_search: true,
          cost_usd: 0.03,
        },
        {
          ...answer,
          model: "claude",
          status: "failed",
          error: "provider_rejected",
        },
        {
          ...answer,
          model: "gemini",
          status: "failed",
          error: "unsupported_country",
        },
      ],
    });
    setup("/workbench/seo-workbench.main?project=p1&view=ai");
    const card = (await screen.findByText("seo.promptTitle")).closest(
      ".ant-card",
    ) as HTMLElement;
    expect(await within(card).findByText("seo.lookupNotBought")).toBeVisible();
    expect(seoApi.aiLookups).toHaveBeenCalledWith("p1", "prompt");
    fireEvent.change(within(card).getByLabelText("seo.promptLabel"), {
      target: { value: "  best free apps " },
    });
    fireEvent.click(
      within(card).getByRole("checkbox", { name: "seo.llm_gemini" }),
    );
    fireEvent.click(
      within(card).getByRole("button", { name: "seo.promptBuy" }),
    );
    expect(
      await screen.findByText("seo.promptPaidConfirm"),
    ).toBeInTheDocument();
    expect(seoApi.buyPromptLookup).not.toHaveBeenCalled();
    const confirm = await screen.findAllByRole("button", {
      name: "seo.serpConfirmBuy",
    });
    await act(async () => {
      fireEvent.click(confirm[confirm.length - 1]);
    });
    expect(seoApi.buyPromptLookup).toHaveBeenCalledWith("p1", {
      prompt: "best free apps",
      models: ["chat_gpt", "gemini"],
      web_search: true,
      idempotency_key: expect.any(String),
      confirm_paid: true,
    });
    // Model output stays plain text: no markup is rendered.
    expect(
      await within(card).findByText("**Example** is great.<img src='x'>"),
    ).toBeVisible();
    expect(card.querySelector("img[src='x']")).toBeNull();
    expect(within(card).getByText("seo.promptMentioned")).toBeVisible();
    expect(within(card).getByText("seo.promptCited")).toBeVisible();
    expect(
      within(card).getByRole("link", { name: "Example top" }),
    ).toHaveAttribute("href", "https://example.com/top");
    expect(within(card).queryByRole("link", { name: "Bad link" })).toBeNull();
    expect(within(card).getByText("free drama apps")).toBeVisible();
    expect(within(card).getByText("seo.promptFailed")).toBeVisible();
    expect(
      within(card).getByText("seo.promptError_unsupported_country"),
    ).toBeVisible();
    // The brand is matched on the page: changing it buys nothing.
    fireEvent.change(within(card).getByLabelText("seo.promptBrand"), {
      target: { value: "nomatch" },
    });
    expect(within(card).getByText("seo.promptNotMentioned")).toBeVisible();
    expect(seoApi.buyPromptLookup).toHaveBeenCalledTimes(1);
  });

  it("looks up a brand with competitors on a confirmed click and shows each section", async () => {
    vi.mocked(seoApi.aiVisibility).mockRejectedValue(new Error("not needed"));
    vi.mocked(seoApi.buyBrandLookup).mockResolvedValue({
      lookup_id: "l-brand",
      kind: "brand",
      request: {
        target: "example.com",
        target_type: "domain",
        competitors: ["Rival", "Other Brand"],
      },
      overview: {
        chatgpt_counted: false,
        total_mentions: 5,
        total_ai_search_volume: 1500,
        platforms: [
          {
            platform: "chat_gpt",
            status: "completed",
            mentions: 10,
            ai_search_volume: 1000,
          },
          {
            platform: "google",
            status: "failed",
            mentions: null,
            ai_search_volume: null,
          },
        ],
        monthly_volume: [
          { year: 2026, month: 8, volume: 100 },
          { year: 2026, month: 9, volume: 200 },
        ],
        share_of_voice: {
          platforms: ["google"],
          entries: [
            {
              label: "example.com",
              is_target: true,
              mentions: 6,
              share_pct: 75,
            },
            { label: "Rival", is_target: false, mentions: 2, share_pct: 25 },
            {
              label: "Other Brand",
              is_target: false,
              mentions: null,
              share_pct: null,
            },
          ],
        },
      },
      status: "completed",
      observed_at: "2026-10-23T08:00:00Z",
      item_count: 2,
      cost_usd: 1.02,
      cached: false,
      items: [
        {
          section: "page",
          platform: "chat_gpt",
          url: "https://example.com/a",
          domain: "example.com",
          mentions: 10,
          captured_volume: 300,
          keywords: [{ question: "best drama apps", ai_search_volume: 900 }],
        },
        {
          section: "query",
          platform: "chat_gpt",
          question: "best drama apps",
          ai_search_volume: 900,
          first_seen_at: null,
          last_seen_at: "2026-10-01 00:00:00 +00:00",
          cited_sources: [
            { url: "javascript:alert(1)", domain: null, title: "Bad source" },
          ],
          brands_mentioned: ["Example"],
        },
      ],
    });
    setup("/workbench/seo-workbench.main?project=p1&view=ai");
    const card = (await screen.findByText("seo.brandTitle")).closest(
      ".ant-card",
    ) as HTMLElement;
    expect(await within(card).findByText("seo.lookupNotBought")).toBeVisible();
    expect(seoApi.aiLookups).toHaveBeenCalledWith("p1", "brand");
    // The project's domain is the default target.
    expect(within(card).getByLabelText("seo.brandTarget")).toHaveValue(
      "example.com",
    );
    fireEvent.change(
      within(card).getByRole("combobox", { name: "seo.brandCompetitors" }),
      { target: { value: " Rival ,Other Brand," } },
    );
    fireEvent.click(within(card).getByRole("button", { name: "seo.brandBuy" }));
    expect(await screen.findByText("seo.brandPaidConfirm")).toBeInTheDocument();
    expect(seoApi.buyBrandLookup).not.toHaveBeenCalled();
    const confirm = await screen.findAllByRole("button", {
      name: "seo.serpConfirmBuy",
    });
    await act(async () => {
      fireEvent.click(confirm[confirm.length - 1]);
    });
    expect(seoApi.buyBrandLookup).toHaveBeenCalledWith("p1", {
      target: "example.com",
      competitors: ["Rival", "Other Brand"],
      idempotency_key: expect.any(String),
      confirm_paid: true,
    });
    expect(await within(card).findByText("1,500")).toBeVisible();
    expect(within(card).getByText("seo.brandPlatformFailed")).toBeVisible();
    expect(within(card).getByText("seo.brandChatgptApart")).toBeVisible();
    expect(within(card).getByText("75.0%")).toBeVisible();
    expect(
      within(card).getByRole("link", { name: "https://example.com/a" }),
    ).toHaveAttribute("href", "https://example.com/a");
    fireEvent.click(within(card).getByText("seo.brandQueriesTab"));
    expect(await within(card).findByText("best drama apps")).toBeVisible();
    fireEvent.click(
      within(card)
        .getAllByRole("button", { name: /expand row/i })
        .at(-1)!,
    );
    expect(await within(card).findByText("Bad source")).toBeVisible();
    expect(within(card).queryByRole("link", { name: "Bad source" })).toBeNull();
    expect(within(card).getByText("Example")).toBeVisible();
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
