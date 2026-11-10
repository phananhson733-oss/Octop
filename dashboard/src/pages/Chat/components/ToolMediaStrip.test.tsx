import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const requestBlob = vi.fn();

vi.mock("../../../api/request", () => ({
  requestBlob: (...args: unknown[]) => requestBlob(...args),
  getAuthToken: () => "t",
}));

import { ToolMediaStrip } from "./ToolMediaStrip";

const VIDEO = {
  url: "/api/agents/A1/media/preview?path=out%2Fclip_1.mp4",
  filename: "clip_1.mp4",
  kind: "video" as const,
};

// The preview fetches the whole MP4 first; the download button must not wait
// for it, nor vanish when it fails (bare grey file names, 2026-11-09).
describe("ToolMediaStrip videos", () => {
  it("offers a download while the preview is still loading", () => {
    requestBlob.mockReturnValue(new Promise(() => {}));

    render(<ToolMediaStrip images={[]} videos={[VIDEO]} agentId="A1" />);

    expect(screen.getByText("clip_1.mp4")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "common.download" }),
    ).toBeTruthy();
  });

  it("keeps the download when the preview fails", async () => {
    requestBlob.mockRejectedValue(new Error("401"));

    render(<ToolMediaStrip images={[]} videos={[VIDEO]} agentId="A1" />);
    await act(async () => {});

    expect(screen.getByText("clip_1.mp4")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "common.download" }),
    ).toBeTruthy();
    expect(document.querySelector("video")).toBeNull();
  });

  it("shows the player and the download once the preview loads", async () => {
    requestBlob.mockResolvedValue(new Blob(["x"], { type: "video/mp4" }));
    URL.createObjectURL = vi.fn(() => "blob:clip");
    URL.revokeObjectURL = vi.fn();

    render(<ToolMediaStrip images={[]} videos={[VIDEO]} agentId="A1" />);
    await act(async () => {});

    expect(document.querySelector("video")?.getAttribute("src")).toBe(
      "blob:clip",
    );
    expect(
      screen.getByRole("button", { name: "common.download" }),
    ).toBeTruthy();
  });
});
