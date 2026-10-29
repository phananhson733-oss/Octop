import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ReportEvidenceButton } from "./ReportEvidenceButton";
import { seoApi, type SeoReport } from "../../../api/modules/seo";
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("../../../api/modules/seo", () => ({
  seoApi: { reportBundle: vi.fn() },
}));
const report: SeoReport = {
  report_id: "r",
  project_id: "p",
  context_id: "c",
  run_id: null,
  title: "Report",
  summary: "",
  skill: "comprehensive",
  created_at: "2026-10-29T00:00:00Z",
  content_hash: "hash",
  size_bytes: 10,
};
describe("Frozen report shortlist", () => {
  it("shows the saved membership and tags from the bundle", async () => {
    vi.mocked(seoApi.reportBundle).mockResolvedValue({
      context_id: "c",
      context_hash: "hash",
      report_type: "comprehensive",
      created_at: report.created_at,
      project_context: {},
      sources: [],
      saved_keywords: [
        {
          keyword_id: "k",
          keyword: "selected-word",
          tags: ["priority-marker"],
          search_volume: 10,
          volume_source: "keyword_info",
          cpc: null,
          competition: null,
          keyword_difficulty: null,
          intent: "unknown",
          trend: [],
          location_code: 2840,
          language_code: "en",
          observed_at: report.created_at,
          source_run_id: "run",
        },
      ],
    });
    render(<ReportEvidenceButton projectId="p" report={report} />);
    fireEvent.click(screen.getByRole("button", { name: "seo.reportEvidence" }));
    fireEvent.click(await screen.findByText("seo.frozenShortlist"));
    await waitFor(() =>
      expect(screen.getByText(/priority-marker/)).toBeVisible(),
    );
  });
  it("labels old bundles as not captured instead of fetching current keywords", async () => {
    vi.mocked(seoApi.reportBundle).mockResolvedValue({
      context_id: "c",
      context_hash: "hash",
      report_type: "comprehensive",
      created_at: report.created_at,
      project_context: {},
      sources: [],
    });
    render(<ReportEvidenceButton projectId="p" report={report} />);
    fireEvent.click(screen.getByRole("button", { name: "seo.reportEvidence" }));
    await waitFor(() =>
      expect(screen.getByText("seo.shortlistNotCaptured")).toBeVisible(),
    );
  });
});
