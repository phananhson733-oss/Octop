import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ActionsPanel } from "./ActionsPanel";
import { seoApi } from "../../../api/modules/seo";
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("../../../api/modules/seo", () => ({
  seoApi: {
    actions: vi.fn(),
    audits: vi.fn(),
    updateAction: vi.fn(),
    verifyAction: vi.fn(),
  },
}));
const action = {
  action_id: "a",
  project_id: "p",
  version: 1,
  status: "open",
  source: {
    audit_id: "old",
    issue_type: "missing-title",
    page_url: "https://example.com/",
  },
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  implementation: null,
  verification: null,
  history: [],
};
describe("SEO actions", () => {
  it("requires an implementation note before recording completion", async () => {
    vi.mocked(seoApi.actions).mockResolvedValue([action]);
    vi.mocked(seoApi.audits).mockResolvedValue([]);
    vi.mocked(seoApi.updateAction).mockResolvedValue({
      ...action,
      status: "implemented",
      implementation: { note: "change abc", at: action.updated_at },
    });
    render(<ActionsPanel projectId="p" isVisible />);
    await screen.findByText("https://example.com/");
    fireEvent.click(screen.getByText("seo.actions.implemented"));
    expect(seoApi.updateAction).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("seo.actions.note"), {
      target: { value: "change abc" },
    });
    fireEvent.click(screen.getByText("seo.actions.implemented"));
    await waitFor(() =>
      expect(seoApi.updateAction).toHaveBeenCalledWith("p", "a", {
        status: "implemented",
        note: "change abc",
      }),
    );
  });
});
