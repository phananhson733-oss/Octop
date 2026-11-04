import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../request";
import { octopThreadsApi } from "./octopThreads";

vi.mock("../request", () => ({
  request: vi.fn(),
}));

const PAGE = { messages: [], has_more: false };

describe("octopThreadsApi.history", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(request).mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("retries a dropped connection and a gateway 502 before giving up", async () => {
    vi.mocked(request)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(new Error("Request failed: 502 Bad Gateway"))
      .mockResolvedValueOnce(PAGE);
    const pending = octopThreadsApi.history("ag", "thr", { limit: 25 });
    await vi.advanceTimersByTimeAsync(3000);
    await expect(pending).resolves.toBe(PAGE);
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("does not retry a 404", async () => {
    vi.mocked(request).mockRejectedValueOnce(
      new Error("Request failed: 404 Not Found"),
    );
    await expect(octopThreadsApi.history("ag", "thr")).rejects.toThrow("404");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("throws after the last retry", async () => {
    vi.mocked(request).mockRejectedValue(new TypeError("Failed to fetch"));
    const pending = octopThreadsApi.history("ag", "thr");
    const settled = expect(pending).rejects.toThrow("Failed to fetch");
    await vi.advanceTimersByTimeAsync(7000);
    await settled;
    expect(request).toHaveBeenCalledTimes(4);
  });
});
