import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const requestBlob = vi.fn();

vi.mock("../../../api/request", () => ({
  requestBlob: (...args: unknown[]) => requestBlob(...args),
  getAuthToken: () => "t",
}));

import { ToolMediaStrip } from "./ToolMediaStrip";

const VIDEO = {
  url: "/api/agents/A1/media/preview?source=out%2Fclip_1.mp4",
  filename: "clip_1.mp4",
  kind: "video" as const,
};

// Clips used to be pulled in full for an inline player as soon as the chat
// opened: bare grey names for minutes on a slow link and no download button
// (2026-11-10). Now they are a download card and nothing loads until clicked.
describe("ToolMediaStrip videos", () => {
  it("shows a download card and fetches nothing up front", () => {
    render(<ToolMediaStrip images={[]} videos={[VIDEO]} agentId="A1" />);

    expect(screen.getByText("clip_1.mp4")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "common.download" }),
    ).toBeTruthy();
    expect(document.querySelector("video")).toBeNull();
    expect(requestBlob).not.toHaveBeenCalled();
  });
});
