import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ToolDetailsInline } from "../../../pages/Chat/components/MessageBubble";
import type { ChatMessage } from "../../../pages/Chat/hooks/useChat";
import { ensureBuiltinToolRenderers } from "../ensureBuiltins";
import { isPinnedToolUiMessage } from "../isPinnedToolUi";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "zh" } }),
}));

const output = JSON.stringify({
  report_id: "01REPORT",
  project_id: "01PROJECT",
  run_id: "01RUN",
  title: "Short-drama keyword report",
  workbench_path:
    "/workbench/seo-workbench.main?project=01PROJECT&view=reports",
});

function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
}

describe("seo_save_report in chat", () => {
  ensureBuiltinToolRenderers();

  it("pins the saved report outside the folded tool list", () => {
    const message = {
      id: "tool-1",
      role: "assistant",
      content: "",
      timestamp: 0,
      toolData: { name: "seo_save_report", callId: "c1", output },
    } as ChatMessage;
    expect(isPinnedToolUiMessage(message)).toBe(true);
  });

  it("opens the saved report in the SEO workbench", () => {
    render(
      <MemoryRouter initialEntries={["/chat/a1/thr1"]}>
        <ToolDetailsInline
          toolData={{ name: "seo_save_report", callId: "c1", output }}
          isStreaming={false}
        />
        <Location />
      </MemoryRouter>,
    );
    expect(screen.getByText("Short-drama keyword report")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /seo\.openReport/ }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/workbench/seo-workbench.main?project=01PROJECT&view=reports&report=01REPORT",
    );
  });

  it("shows no link while the report is still being written", () => {
    render(
      <MemoryRouter>
        <ToolDetailsInline
          toolData={{
            name: "seo_save_report",
            callId: "c1",
            arguments: '{"html": "<html>',
          }}
          isStreaming
        />
      </MemoryRouter>,
    );
    expect(
      screen.queryByRole("button", { name: /seo\.openReport/ }),
    ).not.toBeInTheDocument();
  });
});
