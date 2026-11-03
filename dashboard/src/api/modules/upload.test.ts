import { beforeEach, describe, expect, it, vi } from "vitest";
import { request, requestUpload } from "../request";
import { CHUNKED_UPLOAD_THRESHOLD, uploadFile } from "./upload";

vi.mock("../request", () => ({
  request: vi.fn(),
  requestUpload: vi.fn(),
}));

const DONE = {
  path: "inbound/1_big.mp4",
  workspace_path: "inbound/1_big.mp4",
  filename: "big.mp4",
  media_type: "video/mp4",
  url: "",
  access_url: "/x",
};

const RESUME_KEY = (size: number) =>
  `octop.chunkedUpload:ag|big.mp4|${size}|1700000000000`;

/** A File-like object whose size crosses the threshold without allocating it. */
function bigFile(size: number): File {
  return {
    name: "big.mp4",
    type: "video/mp4",
    size,
    lastModified: 1700000000000,
    slice: (start: number, end: number) => ({ size: end - start, start }),
  } as unknown as File;
}

function status(id: string, size: number, chunk: number, received: number[]) {
  return {
    upload_id: id,
    size,
    chunk_size: chunk,
    chunk_count: Math.ceil(size / chunk),
    received,
  };
}

describe("uploadFile", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(request).mockReset();
    vi.mocked(requestUpload).mockReset();
    localStorage.clear();
  });

  it("sends small files in one multipart request", async () => {
    vi.mocked(requestUpload).mockResolvedValue(DONE);
    const file = new File(["abc"], "a.txt", { type: "text/plain" });
    await expect(uploadFile("ag", file)).resolves.toEqual(DONE);
    expect(vi.mocked(requestUpload).mock.calls[0][0]).toBe("/agents/ag/upload");
    expect(request).not.toHaveBeenCalled();
  });

  it("chunks large files, retries a failed chunk and reports bytes", async () => {
    const chunk = 16 * 1024 * 1024;
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (path === "/agents/ag/upload/chunked") {
        expect(JSON.parse(String(options?.body))).toEqual({
          filename: "big.mp4",
          size,
          content_type: "video/mp4",
        });
        return status("U1", size, chunk, []);
      }
      if (path === "/agents/ag/upload/chunked/U1/complete") return DONE;
      throw new Error(`unexpected ${path}`);
    });
    let failures = 1;
    vi.mocked(requestUpload).mockImplementation(async () => {
      if (failures-- > 0) throw new Error("Network error");
      return {};
    });
    const progress: number[] = [];
    const pending = uploadFile("ag", bigFile(size), (b) => progress.push(b));
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual(DONE);

    const calls = vi.mocked(requestUpload).mock.calls;
    expect(calls.map((c) => c[0]).sort()).toEqual([
      "/agents/ag/upload/chunked/U1/chunks/0",
      "/agents/ag/upload/chunked/U1/chunks/0",
      "/agents/ag/upload/chunked/U1/chunks/1",
      "/agents/ag/upload/chunked/U1/chunks/2",
    ]);
    const last = calls.find((c) => String(c[0]).endsWith("/chunks/2"));
    expect((last?.[1] as Blob).size).toBe(1);
    expect(progress.at(-1)).toBe(size);
    expect(localStorage.getItem(RESUME_KEY(size))).toBeNull();
  });

  it("keeps at most three chunks in flight", async () => {
    const chunk = 8 * 1024 * 1024;
    const size = chunk * 10;
    vi.mocked(request).mockImplementation(async (path) => {
      if (path === "/agents/ag/upload/chunked")
        return status("U3", size, chunk, []);
      return DONE;
    });
    let active = 0;
    let peak = 0;
    vi.mocked(requestUpload).mockImplementation(async () => {
      active++;
      peak = Math.max(peak, active);
      await new Promise((resolve) => setTimeout(resolve, 10));
      active--;
      return {};
    });
    const pending = uploadFile("ag", bigFile(size));
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual(DONE);
    expect(requestUpload).toHaveBeenCalledTimes(10);
    expect(peak).toBe(3);
  });

  it("resumes a remembered session and sends only missing chunks", async () => {
    const chunk = 16 * 1024 * 1024;
    const size = chunk * 3;
    localStorage.setItem(
      RESUME_KEY(size),
      JSON.stringify({ id: "U4", at: Date.now() }),
    );
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (path === "/agents/ag/upload/chunked/U4" && !options) {
        return status("U4", size, chunk, [0, 2]);
      }
      if (path === "/agents/ag/upload/chunked/U4/complete") return DONE;
      throw new Error(`unexpected ${path}`);
    });
    vi.mocked(requestUpload).mockResolvedValue({});
    const progress: number[] = [];
    const pending = uploadFile("ag", bigFile(size), (b) => progress.push(b));
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual(DONE);
    expect(vi.mocked(requestUpload).mock.calls.map((c) => c[0])).toEqual([
      "/agents/ag/upload/chunked/U4/chunks/1",
    ]);
    expect(progress[0]).toBe(chunk * 2);
    expect(localStorage.getItem(RESUME_KEY(size))).toBeNull();
  });

  it("starts over when the remembered session is gone", async () => {
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    localStorage.setItem(
      RESUME_KEY(size),
      JSON.stringify({ id: "OLD", at: Date.now() }),
    );
    vi.mocked(request).mockImplementation(async (path) => {
      if (path === "/agents/ag/upload/chunked/OLD") {
        throw new Error("Request failed: 404 - UPLOAD_NOT_FOUND");
      }
      if (path === "/agents/ag/upload/chunked")
        return status("U5", size, size, []);
      if (path === "/agents/ag/upload/chunked/U5/complete") return DONE;
      throw new Error(`unexpected ${path}`);
    });
    vi.mocked(requestUpload).mockResolvedValue({});
    const pending = uploadFile("ag", bigFile(size));
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual(DONE);
    expect(vi.mocked(requestUpload).mock.calls[0][0]).toBe(
      "/agents/ag/upload/chunked/U5/chunks/0",
    );
  });

  it("keeps the remembered session when its status check fails", async () => {
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    localStorage.setItem(
      RESUME_KEY(size),
      JSON.stringify({ id: "U8", at: Date.now() }),
    );
    vi.mocked(request).mockRejectedValue(
      new Error("Request failed: 503 Service Unavailable"),
    );
    await expect(uploadFile("ag", bigFile(size))).rejects.toThrow("503");
    expect(request).toHaveBeenCalledTimes(1);
    expect(JSON.parse(localStorage.getItem(RESUME_KEY(size)) ?? "{}").id).toBe(
      "U8",
    );
  });

  it("settles every chunk before reporting a failed upload", async () => {
    const chunk = 16 * 1024 * 1024;
    const size = chunk * 3;
    vi.mocked(request).mockImplementation(async (path) => {
      if (path === "/agents/ag/upload/chunked")
        return status("U9", size, chunk, []);
      throw new Error(`unexpected ${path}`);
    });
    vi.mocked(requestUpload).mockImplementation(
      async (path, _b, _o, onProgress) => {
        if (String(path).endsWith("/chunks/0"))
          throw new Error("Network error");
        await new Promise((resolve) => setTimeout(resolve, 60_000));
        onProgress?.(100);
        return {};
      },
    );
    let rejected = false;
    let lateProgress = 0;
    const pending = uploadFile("ag", bigFile(size), () => {
      if (rejected) lateProgress++;
    }).catch((err: Error) => {
      rejected = true;
      return err;
    });
    await vi.runAllTimersAsync();
    expect(String(await pending)).toContain("Network error");
    expect(lateProgress).toBe(0);
  });

  it("recovers the result when the complete reply was lost", async () => {
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    let completes = 0;
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (path === "/agents/ag/upload/chunked")
        return status("U6", size, size, []);
      if (path === "/agents/ag/upload/chunked/U6/complete") {
        if (completes++ === 0) throw new Error("Network error");
        return DONE;
      }
      if (path === "/agents/ag/upload/chunked/U6" && !options) {
        return status("U6", size, size, [0]);
      }
      throw new Error(`unexpected ${path}`);
    });
    vi.mocked(requestUpload).mockResolvedValue({});
    const pending = uploadFile("ag", bigFile(size));
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual(DONE);
    expect(requestUpload).toHaveBeenCalledTimes(1);
  });

  it("keeps the session for resume when it gives up", async () => {
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    let completes = 0;
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (path === "/agents/ag/upload/chunked")
        return status("U2", size, size, []);
      if (path === "/agents/ag/upload/chunked/U2/complete") {
        completes++;
        throw new Error("Request failed: 409 - UPLOAD_INCOMPLETE");
      }
      if (path === "/agents/ag/upload/chunked/U2" && !options) {
        return status("U2", size, size, []);
      }
      return undefined;
    });
    vi.mocked(requestUpload).mockResolvedValue({});
    await expect(uploadFile("ag", bigFile(size))).rejects.toThrow("409");
    expect(completes).toBe(2);
    expect(requestUpload).toHaveBeenCalledTimes(2);
    const methods = vi
      .mocked(request)
      .mock.calls.map((c) => (c[1] as RequestInit | undefined)?.method);
    expect(methods).not.toContain("DELETE");
    expect(JSON.parse(localStorage.getItem(RESUME_KEY(size)) ?? "{}").id).toBe(
      "U2",
    );
  });

  it("refreshes the remembered session on every stored chunk", async () => {
    const chunk = 16 * 1024 * 1024;
    const size = chunk * 3;
    vi.mocked(request).mockImplementation(async (path) => {
      if (path === "/agents/ag/upload/chunked")
        return status("U10", size, chunk, []);
      throw new Error("Request failed: 409 - UPLOAD_INCOMPLETE");
    });
    const seen: number[] = [];
    vi.mocked(requestUpload).mockImplementation(async () => {
      vi.advanceTimersByTime(60_000);
      return {};
    });
    const pending = uploadFile("ag", bigFile(size)).catch(() => undefined);
    await vi.runAllTimersAsync();
    await pending;
    seen.push(JSON.parse(localStorage.getItem(RESUME_KEY(size)) ?? "{}").at);
    expect(seen[0]).toBeGreaterThanOrEqual(Date.now() - 60_000);
  });

  it("forgets remembered sessions older than a day", async () => {
    localStorage.setItem(
      "octop.chunkedUpload:ag|old.mp4|1|1",
      JSON.stringify({ id: "X", at: Date.now() - 25 * 60 * 60 * 1000 }),
    );
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    vi.mocked(request).mockImplementation(async (path) => {
      if (path === "/agents/ag/upload/chunked")
        return status("U7", size, size, []);
      return DONE;
    });
    vi.mocked(requestUpload).mockResolvedValue({});
    const pending = uploadFile("ag", bigFile(size));
    await vi.runAllTimersAsync();
    await pending;
    expect(
      localStorage.getItem("octop.chunkedUpload:ag|old.mp4|1|1"),
    ).toBeNull();
  });
});
