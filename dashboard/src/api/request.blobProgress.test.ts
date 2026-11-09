import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));

vi.mock("../i18n", () => ({
  default: { language: "zh" },
}));

function chunkedResponse(headers: Record<string, string> = {}): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array([1, 2, 3]));
      controller.enqueue(new Uint8Array([4, 5]));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers });
}

describe("requestBlob progress", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports loaded and total bytes when Content-Length is known", async () => {
    vi.resetModules();
    const { requestBlob } = await import("./request");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(chunkedResponse({ "content-length": "5" })),
    );
    const onProgress = vi.fn();

    const blob = await requestBlob("/file", {}, onProgress);

    expect(blob.size).toBe(5);
    expect(onProgress.mock.calls).toEqual([
      [3, 5],
      [5, 5],
    ]);
  });

  // Proxies that compress the body drop Content-Length; chat downloads still
  // need a running byte count instead of a silent spinner.
  it("still reports loaded bytes with total 0 when the size is unknown", async () => {
    vi.resetModules();
    const { requestBlob } = await import("./request");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(chunkedResponse()));
    const onProgress = vi.fn();

    const blob = await requestBlob("/file", {}, onProgress);

    expect(blob.size).toBe(5);
    expect(onProgress.mock.calls).toEqual([
      [3, 0],
      [5, 0],
    ]);
  });
});
