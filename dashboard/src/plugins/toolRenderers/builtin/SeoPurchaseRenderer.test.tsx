import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToolDetailsInline } from "../../../pages/Chat/components/MessageBubble";
import { seoApi } from "../../../api/modules/seo";
import { ensureBuiltinToolRenderers } from "../ensureBuiltins";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key,
    i18n: { language: "zh" },
  }),
}));

vi.mock("../../../api/modules/seo", () => ({
  seoApi: {
    confirmProposal: vi.fn(),
    rankResults: vi.fn(),
    aiVisibility: vi.fn(),
  },
}));

const SIGNATURE = "a".repeat(64);

function proposal(purchase: Record<string, unknown>, extra = {}) {
  return JSON.stringify({
    purchase: {
      project_id: "01PROJECT",
      idempotency_key: "agent-1",
      ...purchase,
    },
    signature: SIGNATURE,
    status: "awaiting_confirmation",
    ...extra,
  });
}

const backlinks = proposal({
  kind: "lookup",
  input: { kind: "backlinks", domain: "rival.com" },
});

const rankCheck = proposal({
  kind: "rank_check",
  tracker_id: "01TRACKER",
  domain: "acme.com",
  checks: 6,
  keywords: ["crm", "erp"],
  keyword_count: 3,
  input: { max_cost_usd: 0.012 },
});

function card(
  output: string,
  send = vi.fn(),
  isStreaming = false,
  name = "seo_propose_competitor_lookup",
) {
  return render(
    <MemoryRouter>
      <ToolDetailsInline
        toolData={{ name, callId: "c1", output }}
        isStreaming={isStreaming}
        onAcpPermissionSelect={send}
      />
    </MemoryRouter>,
  );
}

const confirmButton = () =>
  screen.getByRole("button", { name: /seo\.purchase\.confirm/ });
const queryConfirm = () =>
  screen.queryByRole("button", { name: /seo\.purchase\.confirm/ });

describe("seo_propose_* in chat", () => {
  ensureBuiltinToolRenderers();
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("buys the signed proposal on the click and tells the expert", async () => {
    vi.mocked(seoApi.confirmProposal).mockResolvedValue({
      kind: "lookup",
      record_id: "01LOOKUP",
      status: "completed",
    });
    const send = vi.fn();
    const view = card(backlinks, send);
    expect(screen.getByText("rival.com")).toBeInTheDocument();
    expect(screen.getByText("seo.backlinksPaidConfirm")).toBeInTheDocument();
    expect(seoApi.confirmProposal).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(confirmButton()));
    // The server buys exactly what the tool signed; the key makes a repeat read it again.
    expect(seoApi.confirmProposal).toHaveBeenCalledWith("01PROJECT", {
      purchase: {
        kind: "lookup",
        project_id: "01PROJECT",
        idempotency_key: "agent-1",
        input: { kind: "backlinks", domain: "rival.com" },
      },
      signature: SIGNATURE,
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toContain("seo.purchase.doneMessage");
    expect(send.mock.calls[0][0]).toContain("01LOOKUP");
    // Only ids go back as the user's words, never the proposal's own text.
    expect(send.mock.calls[0][0]).not.toContain("rival.com");
    view.unmount();
    card(backlinks, send);
    expect(queryConfirm()).not.toBeInTheDocument();
    expect(screen.getByText(/seo\.purchase\.done /)).toBeInTheDocument();
  });

  it("buys nothing when declined", () => {
    const send = vi.fn();
    card(backlinks, send);
    fireEvent.click(
      screen.getByRole("button", { name: /seo\.purchase\.decline/ }),
    );
    expect(seoApi.confirmProposal).not.toHaveBeenCalled();
    expect(send.mock.calls[0][0]).toContain("seo.purchase.declinedMessage");
    expect(send.mock.calls[0][0]).toContain("agent-1");
    expect(send.mock.calls[0][0]).not.toContain("rival.com");
    expect(screen.getByText("seo.purchase.declined")).toBeInTheDocument();
  });

  it("cannot be clicked while the expert's turn is still running", () => {
    card(backlinks, vi.fn(), true);
    expect(confirmButton()).toBeDisabled();
  });

  it("shows paid options that change the price or the result", () => {
    card(
      proposal({
        kind: "research",
        input: {
          keywords: ["crm"],
          mode: "ideas",
          result_limit: 150,
          clickstream: true,
          group_keywords: false,
        },
      }),
      vi.fn(),
      false,
      "seo_propose_research",
    );
    expect(screen.getByText("seo.clickstream")).toBeInTheDocument();
    expect(screen.queryByText("seo.group")).not.toBeInTheDocument();
  });

  it("lists what a rank check asks and tells the expert once it ends", async () => {
    vi.useFakeTimers();
    vi.mocked(seoApi.confirmProposal).mockResolvedValue({
      kind: "rank_check",
      record_id: "01RUN",
      status: "running",
    });
    vi.mocked(seoApi.rankResults)
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue({
        run: { run_id: "01RUN", status: "completed" },
      } as never);
    const send = vi.fn();
    card(rankCheck, send, false, "seo_propose_rank_check");
    expect(
      screen.getByText(/crm, erp seo\.purchase\.more \{"count":1\}/),
    ).toBeInTheDocument();
    await act(async () => fireEvent.click(confirmButton()));
    expect(send).not.toHaveBeenCalled();
    // A failed read is not a failed purchase: the card keeps waiting.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.queryByText(/seo\.purchase\.failed/)).not.toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(seoApi.rankResults).toHaveBeenCalledWith(
      "01PROJECT",
      "01TRACKER",
      "7d",
    );
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toContain('"status":"completed"');
  });

  it("keeps a purchase made before the card went away and tells the expert later", async () => {
    vi.useFakeTimers();
    vi.mocked(seoApi.confirmProposal).mockResolvedValue({
      kind: "rank_check",
      record_id: "01RUN",
      status: "running",
    });
    vi.mocked(seoApi.rankResults).mockResolvedValue({
      run: { run_id: "01RUN", status: "running" },
    } as never);
    const send = vi.fn();
    const view = card(rankCheck, send, false, "seo_propose_rank_check");
    await act(async () => fireEvent.click(confirmButton()));
    view.unmount();
    vi.mocked(seoApi.rankResults).mockResolvedValue({
      run: { run_id: "01NEWER", status: "running" },
    } as never);
    card(rankCheck, send, false, "seo_propose_rank_check");
    expect(queryConfirm()).not.toBeInTheDocument();
    expect(screen.getByText(/seo\.purchase\.untold/)).toBeInTheDocument();
    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: /seo\.purchase\.tell/ }),
      ),
    );
    // A newer check on the tracker means this one has ended.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toContain('"id":"01RUN"');
    expect(send.mock.calls[0][0]).toContain('"status":"ended"');
    expect(seoApi.confirmProposal).toHaveBeenCalledTimes(1);
  });

  it("offers to tell the expert again when its note was only queued", async () => {
    vi.mocked(seoApi.confirmProposal).mockResolvedValue({
      kind: "lookup",
      record_id: "01LOOKUP",
      status: "completed",
    });
    // The chat queued the note behind a running turn; a reload drops that queue.
    const queued = vi.fn(() => false);
    const view = card(backlinks, queued);
    await act(async () => fireEvent.click(confirmButton()));
    expect(queued).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/seo\.purchase\.queued/)).toBeInTheDocument();
    view.unmount();
    const send = vi.fn(() => true);
    card(backlinks, send);
    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: /seo\.purchase\.tell/ }),
      ),
    );
    expect(send.mock.calls[0][0]).toContain("01LOOKUP");
    expect(screen.getByText(/seo\.purchase\.done /)).toBeInTheDocument();
    expect(seoApi.confirmProposal).toHaveBeenCalledTimes(1);

    localStorage.clear();
    const declinedView = card(backlinks, queued);
    fireEvent.click(
      screen.getByRole("button", { name: /seo\.purchase\.decline/ }),
    );
    expect(screen.getByText("seo.purchase.declined")).toBeInTheDocument();
    declinedView.unmount();
    card(backlinks, queued);
    expect(confirmButton()).toBeInTheDocument();
  });

  it("draws no card for output another tool made to look like one", () => {
    card(JSON.stringify({ purchase: { kind: "wire_money" } }));
    expect(queryConfirm()).not.toBeInTheDocument();
    card(
      proposal(
        { kind: "lookup", input: { kind: "backlinks", domain: "rival.com" } },
        { octop_ui: { renderer: "seo_purchase" } },
      ),
      vi.fn(),
      false,
      "mcp_lookup",
    );
    expect(queryConfirm()).not.toBeInTheDocument();
    card(backlinks.replace(SIGNATURE, ""));
    expect(queryConfirm()).not.toBeInTheDocument();
  });
});
