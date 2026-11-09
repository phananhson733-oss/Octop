import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));

vi.mock("../i18n", () => ({
  default: { language: "zh" },
}));

describe("auth token remember / session-only", () => {
  let mod: typeof import("./request");

  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    sessionStorage.clear();
    mod = await import("./request");
  });

  it("defaults to a remembered (persistent) token in localStorage", () => {
    mod.setAuthToken("tok");
    expect(mod.getAuthToken()).toBe("tok");
    expect(mod.isSessionOnlyAuth()).toBe(false);
    expect(localStorage.getItem("auth_token")).toBe("tok");
    expect(sessionStorage.getItem("auth_token")).toBeNull();
  });

  it("stores a session-only token in sessionStorage", () => {
    mod.setAuthToken("tok", false);
    expect(mod.getAuthToken()).toBe("tok");
    expect(mod.isSessionOnlyAuth()).toBe(true);
    expect(sessionStorage.getItem("auth_token")).toBe("tok");
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("clears the other store when switching remember mode", () => {
    mod.setAuthToken("a", false);
    mod.setAuthToken("b", true);
    expect(localStorage.getItem("auth_token")).toBe("b");
    expect(sessionStorage.getItem("auth_token")).toBeNull();
    expect(mod.isSessionOnlyAuth()).toBe(false);
  });

  it("preserves session-only mode when renewing the access token", () => {
    mod.setAuthToken("old", false);
    const response = new Response(null, {
      headers: { "X-Octop-Access-Token": "renewed" },
    });
    mod.applyRenewedAccessToken(response);
    expect(mod.getAuthToken()).toBe("renewed");
    expect(mod.isSessionOnlyAuth()).toBe(true);
    expect(sessionStorage.getItem("auth_token")).toBe("renewed");
  });

  it("reads and writes the SSO remember preference", () => {
    expect(mod.getRememberLoginPreference()).toBe(true);
    mod.setRememberLoginPreference(false);
    expect(mod.getRememberLoginPreference()).toBe(false);
  });

  it("clearAuthToken removes both stores", () => {
    mod.setAuthToken("tok", false);
    mod.clearAuthToken();
    expect(mod.getAuthToken()).toBe("");
    expect(sessionStorage.getItem("auth_token")).toBeNull();
  });
});

describe("cross-tab identity boundaries", () => {
  const jwt = (sub: string, sid: string, iat: number) =>
    `header.${btoa(JSON.stringify({ sub, sid, iat }))}.signature`;

  it("keeps renewal in the same session but detects another login", async () => {
    vi.resetModules();
    const api = await import("./request");
    localStorage.clear();
    sessionStorage.clear();
    const first = jwt("1", "session-a", 1);
    const renewed = jwt("1", "session-a", 2);
    api.setAuthToken(first);
    localStorage.setItem("custom-preference", "keep");
    const changed = vi.fn();
    const stop = api.watchAuthSession(changed);
    try {
      api.setAuthToken(renewed);
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "auth_token",
          storageArea: localStorage,
        }),
      );
      expect(changed).not.toHaveBeenCalled();
      api.setAuthToken(jwt("1", "session-b", 3));
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "auth_token",
          storageArea: localStorage,
        }),
      );
      expect(changed).toHaveBeenCalledOnce();
      expect(localStorage.getItem("custom-preference")).toBe("keep");
    } finally {
      stop();
    }
  });

  it.each([200, 401])(
    "ignores a late %s from the previous identity",
    async (status) => {
      vi.resetModules();
      const api = await import("./request");
      localStorage.clear();
      sessionStorage.clear();
      api.setAuthToken("customer-token");
      let finish!: (value: Response) => void;
      const fetchMock = vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      );
      vi.stubGlobal("fetch", fetchMock);
      const changed = vi.fn();
      const stop = api.watchAuthSession(changed);
      try {
        const pending = api.request("/agents");
        api.setAuthToken("internal-token");
        finish(
          new Response(JSON.stringify({ error: { code: "AUTH_FAILED" } }), {
            status,
            headers: {
              "content-type": "application/json",
              "X-Octop-Access-Token": "old-renewal",
            },
          }),
        );
        await expect(pending).rejects.toMatchObject({ name: "AbortError" });
        expect(api.getAuthToken()).toBe("internal-token");
        expect(changed).toHaveBeenCalledOnce();
        await expect(api.request("/agents")).rejects.toMatchObject({
          name: "AbortError",
        });
        expect(fetchMock).toHaveBeenCalledOnce();
      } finally {
        stop();
        vi.unstubAllGlobals();
      }
    },
  );
});

it("cancels an old identity stream before another chunk is delivered", async () => {
  vi.resetModules();
  localStorage.clear();
  sessionStorage.clear();
  const api = await import("./request");
  api.setAuthToken("customer-token");
  let source!: ReadableStreamDefaultController<Uint8Array>;
  const canceled = vi.fn();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      source = controller;
    },
    cancel: canceled,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(body, {
        headers: { "content-type": "text/event-stream" },
      }),
    ),
  );
  try {
    const stream = await api.requestStream("/agents/a/chat");
    const reader = stream.body.getReader();
    source.enqueue(new TextEncoder().encode("first"));
    expect(new TextDecoder().decode((await reader.read()).value)).toBe("first");
    api.setAuthToken("internal-token");
    source.enqueue(new TextEncoder().encode("old-private-data"));
    await expect(reader.read()).rejects.toMatchObject({ name: "AbortError" });
    expect(canceled).toHaveBeenCalledOnce();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("a late upload cannot renew or clear a new identity", async () => {
  vi.resetModules();
  localStorage.clear();
  sessionStorage.clear();
  const api = await import("./request");
  api.setAuthToken("customer-token");
  class Upload {
    static last: Upload;
    status = 200;
    responseText = '{"ok":true}';
    upload = {};
    onload: (() => void) | null = null;
    onabort: (() => void) | null = null;
    constructor() {
      Upload.last = this;
    }
    open() {}
    setRequestHeader() {}
    send() {}
    abort() {
      this.onabort?.();
    }
    getResponseHeader() {
      return "old-renewal";
    }
  }
  vi.stubGlobal("XMLHttpRequest", Upload);
  try {
    const pending = api.requestUpload("/agents/a/upload", new Blob(["file"]));
    api.setAuthToken("internal-token");
    Upload.last.onload?.();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(api.getAuthToken()).toBe("internal-token");
  } finally {
    vi.unstubAllGlobals();
  }
});

it("does not prefetch a second identity-bound chunk", async () => {
  vi.resetModules();
  localStorage.clear();
  sessionStorage.clear();
  const api = await import("./request");
  api.setAuthToken("customer-token");
  const canceled = vi.fn();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("first"));
      controller.enqueue(new TextEncoder().encode("old-buffered"));
    },
    cancel: canceled,
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body)));
  try {
    const stream = await api.requestStream("/stream");
    const reader = stream.body.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toBe("first");
    await Promise.resolve();
    await Promise.resolve();
    api.setAuthToken("internal-token");
    await expect(reader.read()).rejects.toMatchObject({ name: "AbortError" });
    expect(canceled).toHaveBeenCalledOnce();
  } finally {
    vi.unstubAllGlobals();
  }
});

it.each([
  "request",
  "requestBlob",
  "requestStream",
  "probeAuthResource",
] as const)("%s drops stale delayed error details", async (method) => {
  vi.resetModules();
  localStorage.clear();
  sessionStorage.clear();
  const api = await import("./request");
  api.setAuthToken("customer-token");
  const response = new Response("private error", { status: 500 });
  vi.spyOn(response, "text").mockImplementation(async () => {
    api.setAuthToken("internal-token");
    return "old-private-error";
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
  try {
    await expect(api[method]("/resource")).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(api.getAuthToken()).toBe("internal-token");
  } finally {
    vi.unstubAllGlobals();
  }
});

it("unified logout preserves legacy and other identity preferences", async () => {
  vi.resetModules();
  localStorage.clear();
  sessionStorage.clear();
  const api = await import("./request");
  api.setAuthStorageIdentity("customer-scope");
  api.setAuthToken("customer-token");
  localStorage.setItem("octop:active-agent", "legacy");
  localStorage.setItem(
    "octop:active-agent:identity:internal-scope",
    "internal-agent",
  );
  api.clearAuthToken();
  expect(api.getAuthToken()).toBe("");
  expect(api.getAuthStorageIdentity()).toBeNull();
  expect(localStorage.getItem("octop:active-agent")).toBe("legacy");
  expect(
    localStorage.getItem("octop:active-agent:identity:internal-scope"),
  ).toBe("internal-agent");
});
