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

/** A File-like object whose size crosses the threshold without allocating it. */
function bigFile(size: number): File {
  return {
    name: "big.mp4",
    type: "video/mp4",
    size,
    slice: (start: number, end: number) => ({ size: end - start, start }),
  } as unknown as File;
}

describe("uploadFile", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(request).mockReset();
    vi.mocked(requestUpload).mockReset();
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
        return {
          upload_id: "U1",
          chunk_size: chunk,
          chunk_count: 3,
          received: [],
        };
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

    const paths = vi.mocked(requestUpload).mock.calls.map((c) => c[0]);
    expect(paths).toEqual([
      "/agents/ag/upload/chunked/U1/chunks/0",
      "/agents/ag/upload/chunked/U1/chunks/0",
      "/agents/ag/upload/chunked/U1/chunks/1",
      "/agents/ag/upload/chunked/U1/chunks/2",
    ]);
    const lastBody = vi.mocked(requestUpload).mock.calls[3][1] as Blob;
    expect(lastBody.size).toBe(1);
    expect(progress.at(-1)).toBe(size);
  });

  it("resends missing chunks when complete fails, and cancels on give-up", async () => {
    const size = CHUNKED_UPLOAD_THRESHOLD + 1;
    let completes = 0;
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (path === "/agents/ag/upload/chunked") {
        return {
          upload_id: "U2",
          chunk_size: size,
          chunk_count: 1,
          received: [],
        };
      }
      if (path === "/agents/ag/upload/chunked/U2/complete") {
        completes++;
        throw new Error("Request failed: 409 - UPLOAD_INCOMPLETE");
      }
      if (path === "/agents/ag/upload/chunked/U2" && !options) {
        return {
          upload_id: "U2",
          chunk_size: size,
          chunk_count: 1,
          received: [],
        };
      }
      return undefined;
    });
    vi.mocked(requestUpload).mockResolvedValue({});
    await expect(uploadFile("ag", bigFile(size))).rejects.toThrow("409");
    expect(completes).toBe(2);
    expect(requestUpload).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenLastCalledWith("/agents/ag/upload/chunked/U2", {
      method: "DELETE",
    });
  });
});
