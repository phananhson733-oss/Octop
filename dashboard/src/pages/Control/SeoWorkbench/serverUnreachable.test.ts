import { describe, expect, it } from "vitest";
import { isServerUnreachable } from "./serverUnreachable";

describe("isServerUnreachable", () => {
  it("treats a proxy 502 and a failed fetch as the server being away", () => {
    expect(
      isServerUnreachable(
        new Error(
          'Request failed: 502 Bad Gateway - {"status":"error","code":502,"message":"Application failed to respond","request_id":"r1"}',
        ),
      ),
    ).toBe(true);
    expect(
      isServerUnreachable(new Error("Request failed: 504 Gateway Timeout")),
    ).toBe(true);
    expect(isServerUnreachable(new TypeError("Failed to fetch"))).toBe(true);
  });

  it("keeps errors that Octop itself answered", () => {
    expect(
      isServerUnreachable(
        new Error(
          'Request failed: 503 Service Unavailable - {"error":{"code":"BRIDGE_NOT_CONNECTED","message":"Bridge offline"}}',
        ),
      ),
    ).toBe(false);
    expect(
      isServerUnreachable(
        new Error(
          'Request failed: 500 Internal Server Error - {"error":{"code":"INTERNAL","message":"boom"}}',
        ),
      ),
    ).toBe(false);
    expect(isServerUnreachable("Request failed: 502")).toBe(false);
  });
});
