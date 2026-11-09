import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const downloadAuthFile = vi.fn();

vi.mock("../../../components/AuthFileDownloadLink", () => ({
  downloadAuthFile: (...args: unknown[]) => downloadAuthFile(...args),
}));

import { MessageFileCard } from "./MessageFileCard";

const FILE_URL = "/api/agents/A1/workspace/download?path=out%2Fclip_1.mp4";

describe("MessageFileCard download progress", () => {
  it("shows the percent while the file streams in, then the icon again", async () => {
    let report: (loaded: number, total: number) => void = () => {};
    let finish: () => void = () => {};
    downloadAuthFile.mockImplementation(
      (
        _url: string,
        opts: { onProgress: (loaded: number, total: number) => void },
      ) => {
        report = opts.onProgress;
        return new Promise<void>((resolve) => {
          finish = resolve;
        });
      },
    );

    render(<MessageFileCard url={FILE_URL} filename="clip_1.mp4" />);
    fireEvent.click(screen.getByRole("button"));

    act(() => report(5_000_000, 20_000_000));
    expect(screen.getByRole("button").textContent).toBe("25%");
    expect(screen.getByRole("button").getAttribute("title")).toContain("25%");

    await act(async () => finish());
    expect(screen.getByRole("button").textContent).toBe("");
    expect(screen.getByRole("button").hasAttribute("disabled")).toBe(false);
  });

  it("shows the bytes received when the size is unknown", async () => {
    let report: (loaded: number, total: number) => void = () => {};
    downloadAuthFile.mockImplementation(
      (
        _url: string,
        opts: { onProgress: (loaded: number, total: number) => void },
      ) => {
        report = opts.onProgress;
        return new Promise<void>(() => {});
      },
    );

    render(<MessageFileCard url={FILE_URL} filename="clip_1.mp4" />);
    fireEvent.click(screen.getByRole("button"));

    act(() => report(3 * 1024 * 1024, 0));
    expect(screen.getByRole("button").textContent).toBe("3.00 MB");
  });
});
