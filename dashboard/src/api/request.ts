import { getApiUrl } from "./config";
import i18n from "../i18n";
import { markNavigatingAway } from "../utils/reloadOnStaleChunk";

const AUTH_TOKEN_KEY = "auth_token";
/** sessionStorage handoff for SSO redirects / popups (default = remember). */
const AUTH_REMEMBER_PREF_KEY = "auth_remember_pref";

/** UI-only continuity check, never a substitute for server token validation. */
export function sameAuthSession(left: string, right: string): boolean {
  if (left === right) return true;
  const claims = (token: string): { sub: string; sid?: string } | null => {
    try {
      const parts = token.split(".");
      if (parts.length !== 3) return null;
      const value = JSON.parse(
        atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")),
      );
      if (typeof value.sub !== "string" && typeof value.sub !== "number")
        return null;
      return {
        sub: String(value.sub),
        sid: typeof value.sid === "string" ? value.sid : undefined,
      };
    } catch {
      return null;
    }
  };
  const a = claims(left),
    b = claims(right);
  if (!a || !b || a.sub !== b.sub) return false;
  return a.sid && b.sid ? a.sid === b.sid : true; // Pre-SID native tokens keep legacy continuity.
}

let authStorageIdentity: string | null = null;
export function setAuthStorageIdentity(identity: string | null): void {
  authStorageIdentity = identity;
}
export function getAuthStorageIdentity(): string | null {
  return authStorageIdentity;
}

let authWatch: { token: string; invalid: boolean; changed: () => void } | null =
  null;

function assertAuthSnapshot(token: string): void {
  const current = getAuthToken();
  if (
    authWatch &&
    (authWatch.invalid || !sameAuthSession(authWatch.token, current))
  ) {
    if (!authWatch.invalid) {
      authWatch.invalid = true;
      // Drop only this tab's shadow credential; never clear another tab's new token or preferences.
      sessionStorage.removeItem(AUTH_TOKEN_KEY);
      authWatch.changed();
    }
    throw new DOMException("Account changed", "AbortError");
  }
  if (!sameAuthSession(token, current))
    throw new DOMException("Account changed", "AbortError");
}

export function watchAuthSession(changed: () => void): () => void {
  const watch = { token: getAuthToken(), invalid: false, changed };
  authWatch = watch;
  const listener = (event: StorageEvent) => {
    if (event.key !== null && event.key !== AUTH_TOKEN_KEY) return;
    if (event.storageArea && event.storageArea !== localStorage) return;
    try {
      assertAuthSnapshot(watch.token);
    } catch {
      /* Guard is already invalidated. */
    }
  };
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener("storage", listener);
    if (authWatch === watch) authWatch = null;
  };
}

async function checkResponseIdentity(
  token: string,
  response: Response,
): Promise<void> {
  try {
    assertAuthSnapshot(token);
  } catch (error) {
    await response.body?.cancel().catch(() => undefined);
    throw error;
  }
}

function identityBoundBody(
  body: ReadableStream<Uint8Array>,
  token: string,
): ReadableStream<Uint8Array> {
  const reader = body.getReader();
  return new ReadableStream<Uint8Array>(
    {
      async pull(controller) {
        try {
          const next = await reader.read();
          assertAuthSnapshot(token);
          if (next.done) {
            reader.releaseLock();
            controller.close();
          } else controller.enqueue(next.value);
        } catch (error) {
          await reader.cancel(error).catch(() => undefined);
          controller.error(error);
        }
      },
      async cancel(reason) {
        await reader.cancel(reason).catch(() => undefined);
      },
    },
    { highWaterMark: 0 },
  );
}

/**
 * Fired when the session is no longer valid. Cancelable: a listener inside the
 * React tree calls ``preventDefault()`` to route to /login itself, which keeps
 * the SPA alive instead of tearing the document down mid-render.
 */
export const UNAUTHORIZED_EVENT = "octop:unauthorized";
/** Fired when an agent-scoped API request is forbidden. */
export const FORBIDDEN_EVENT = "octop:forbidden";

/** Response header used by the server for JWT sliding renewal. */
export const ACCESS_TOKEN_RESPONSE_HEADER = "X-Octop-Access-Token";

/** Quiet error thrown when setup lockdown blocks a non-wizard API call. */
export class SetupRequiredError extends Error {
  constructor() {
    super("Setup required");
    this.name = "SetupRequiredError";
  }
}

/** Session flag: backend reported no admin yet (setup lockdown active). */
let _setupRequiredKnown = false;

/** Mark that first-run setup is still required (skips further locked APIs). */
export function markSetupRequired(): void {
  _setupRequiredKnown = true;
}

/** Clear the setup-lockdown short-circuit (after admin exists / login). */
export function clearSetupRequired(): void {
  _setupRequiredKnown = false;
}

/** Whether this tab already knows setup lockdown is active. */
export function isSetupRequiredKnown(): boolean {
  return _setupRequiredKnown;
}

/** Stash the login-page "remember me" choice for SSO redirects / popups. */
export function setRememberLoginPreference(remember: boolean): void {
  sessionStorage.setItem(AUTH_REMEMBER_PREF_KEY, remember ? "1" : "0");
}

/** Read the stashed remember preference (defaults to true). */
export function getRememberLoginPreference(): boolean {
  return sessionStorage.getItem(AUTH_REMEMBER_PREF_KEY) !== "0";
}

/**
 * Whether the current token is session-only (not "remember me").
 * Session-only tokens live in ``sessionStorage`` so a new tab does not
 * share — or erase — another tab's login.
 */
export function isSessionOnlyAuth(): boolean {
  return (
    sessionStorage.getItem(AUTH_TOKEN_KEY) != null &&
    localStorage.getItem(AUTH_TOKEN_KEY) == null
  );
}

/**
 * Save JWT token. When ``remember`` is false the token is kept in
 * ``sessionStorage`` (cleared when the tab/window closes). SSO popups must
 * post the token back to the opener — ``sessionStorage`` is not shared.
 */
export function setAuthToken(token: string, remember: boolean = true) {
  if (remember) {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
    sessionStorage.removeItem(AUTH_TOKEN_KEY);
  } else {
    sessionStorage.setItem(AUTH_TOKEN_KEY, token);
    localStorage.removeItem(AUTH_TOKEN_KEY);
  }
  clearSetupRequired();
}

/** Get JWT token from localStorage or (session-only) sessionStorage. */
export function getAuthToken(): string {
  return (
    localStorage.getItem(AUTH_TOKEN_KEY) ||
    sessionStorage.getItem(AUTH_TOKEN_KEY) ||
    ""
  );
}

/** Remove JWT token from both storages. */
export function clearAuthToken() {
  const unified = authStorageIdentity !== null;
  authStorageIdentity = null;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  sessionStorage.removeItem(AUTH_TOKEN_KEY);
  sessionStorage.removeItem(AUTH_REMEMBER_PREF_KEY);
  if (!unified) localStorage.removeItem("octop:active-agent");
  setActiveAgentId(null);
}

/** Persist a sliding-renewed access token from an API response, if present. */
export function applyRenewedAccessToken(
  response: Response,
  requestToken: string = getAuthToken(),
): void {
  assertAuthSnapshot(requestToken);
  const renewed = response.headers.get(ACCESS_TOKEN_RESPONSE_HEADER);
  if (renewed) {
    setAuthToken(renewed, !isSessionOnlyAuth());
  }
}

let _redirectingToSetup = false;

/**
 * Hard-redirect once when the backend reports the wizard isn't done.
 * The flag prevents N parallel API calls from each issuing a navigate.
 */
function handleSetupRequired(): void {
  markSetupRequired();
  if (_redirectingToSetup) return;
  // Already on a public bootstrap route — a 503 from a background prefetch
  // must not reload the page or we loop forever.
  const path = window.location.pathname;
  if (
    path.startsWith("/setup") ||
    path.startsWith("/login") ||
    path.startsWith("/invite")
  ) {
    return;
  }
  _redirectingToSetup = true;
  // Full reload drops any in-flight React state.
  window.location.replace("/setup");
}

/** Wizard endpoints stay reachable while lockdown is active. */
function isSetupApiPath(path: string): boolean {
  return path === "/setup/status" || path.startsWith("/setup/");
}

/**
 * Skip the network when this tab already knows setup lockdown is on.
 * Avoids stampeding ``/api/agents`` etc. after the first 503 / status probe.
 */
function assertNotSetupLocked(path: string): void {
  assertAuthSnapshot(getAuthToken());
  if (_setupRequiredKnown && !isSetupApiPath(path)) {
    handleSetupRequired();
    throw new SetupRequiredError();
  }
}

/**
 * Inspect a response for the lockdown signal (503 + body
 * `{setup_required: true}`) and trigger a one-shot navigate to /setup.
 *
 * Returns ``true`` when the response matched and the caller should
 * abort the normal success/error path. The wizard's own ``/setup/*``
 * calls are exempt to avoid redirect loops.
 */
async function check503ForSetupRequired(
  path: string,
  response: Response,
  authSnapshot: string,
): Promise<boolean> {
  if (response.status !== 503) return false;
  let body: unknown = null;
  try {
    body = await response.clone().json();
  } catch {
    /* not JSON — fall through to the standard error path. */
    return false;
  }
  if (
    body &&
    typeof body === "object" &&
    (body as Record<string, unknown>).setup_required === true
  ) {
    assertAuthSnapshot(authSnapshot);
    markSetupRequired();
    clearAuthToken();
    if (!isSetupApiPath(path)) {
      handleSetupRequired();
    }
    return true;
  }
  return false;
}

/**
 * Active agent id — populated by ``AgentProvider`` in ``context/AgentContext.tsx``
 * whenever the user picks a new agent in the top-bar switcher. Stored at
 * module scope so plain functions like ``request()`` can read it without
 * threading a context through every call site.
 *
 * The value is ALSO mirrored to ``localStorage["octop:active-agent"]`` by
 * the provider — but the source of truth at request time is this variable
 * so reactions stay synchronous.
 */
let activeAgentId: string | null = null;

/** Setter used by AgentProvider; also clears when ``null``. */
export function setActiveAgentId(id: string | null) {
  activeAgentId = id;
}

/** Read the active agent id (e.g. from non-React code). */
export function getActiveAgentId(): string | null {
  return activeAgentId;
}

/**
 * Optional ``X-Octop-Agent-Id`` for caller-opted header tunnels (composer
 * GETs, connector lists). Do not auto-attach these on settings pages.
 */
export function bridgeAgentHeaders(
  agentId?: string | null,
): Record<string, string> | undefined {
  const id = (agentId ?? "").trim();
  if (!id) return undefined;
  return { "X-Octop-Agent-Id": id };
}

/**
 * Decide whether a request path is "agent-scoped" — i.e. talking to a
 * concrete agent's resource — and therefore should carry the
 * ``X-Octop-Agent-Id`` header. Health, admin, auth, setup, providers, and
 * personas don't need it.
 *
 * Composer / knowledge / browser host GETs are NOT auto-scoped: those
 * settings pages must keep talking to this instance. Callers that need a
 * remote hop pass ``bridgeAgentHeaders(agentId)`` explicitly.
 */
function isAgentScopedPath(path: string): boolean {
  // Match `/agents/<id>/...` (one trailing segment after the id).
  // The path passed to request() is stripped of the /api prefix.
  if (/^\/agents\/[^/]+(\/|$)/.test(path)) return true;
  // MBTI endpoints that read/write the active agent's persona config.
  if (/^\/mbti\//.test(path)) return true;
  if (path === "/cron/settings") return true;
  if (path === "/subagent-catalog" || path.startsWith("/subagent-catalog/")) {
    return true;
  }
  return false;
}

function buildHeaders(path: string, extra?: HeadersInit): HeadersInit {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Accept-Language": i18n.language?.startsWith("zh") ? "zh" : "en",
  };

  // Apply the global JWT first; the caller's `extra` (including a
  // wizard token) can still override it below.
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  // Caller-supplied headers win — needed so the setup wizard can pass
  // its short-TTL Bearer without being stomped by a stale localStorage
  // JWT.
  if (extra) {
    const extraEntries =
      extra instanceof Headers
        ? Array.from(extra.entries())
        : Array.isArray(extra)
        ? extra
        : Object.entries(extra);
    for (const [k, v] of extraEntries) {
      headers[k] = String(v);
    }
  }

  if (
    activeAgentId &&
    isAgentScopedPath(path) &&
    !headers["X-Octop-Agent-Id"]
  ) {
    headers["X-Octop-Agent-Id"] = activeAgentId;
  }

  return headers;
}

/**
 * Build auth-only headers (no Content-Type — let the browser set it for FormData).
 */
function buildAuthHeaders(path: string): Record<string, string> {
  const headers: Record<string, string> = {
    "Accept-Language": i18n.language?.startsWith("zh") ? "zh" : "en",
  };
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (
    activeAgentId &&
    isAgentScopedPath(path) &&
    !headers["X-Octop-Agent-Id"]
  ) {
    headers["X-Octop-Agent-Id"] = activeAgentId;
  }
  return headers;
}

let _redirectingToLogin = false;

/** 401 codes that mean this Octop session is no longer valid. */
const SESSION_UNAUTHORIZED_CODES = new Set(["AUTH_FAILED", "TOKEN_EXPIRED"]);

/**
 * Send the user to the login screen exactly once, no matter how many parallel
 * requests report an expired session.
 */
function handleUnauthorized(): void {
  if (_redirectingToLogin) return;
  const path = window.location.pathname;
  if (
    path.startsWith("/setup") ||
    path.startsWith("/login") ||
    path.startsWith("/invite")
  )
    return;
  _redirectingToLogin = true;

  const takenOver = !window.dispatchEvent(
    new CustomEvent(UNAUTHORIZED_EVENT, { cancelable: true }),
  );
  if (takenOver) return;

  markNavigatingAway();
  window.location.replace("/login");
}

/**
 * Handle 401 responses: clear token, redirect to login, and throw.
 * Shared by request(), requestBlob(), and requestUpload().
 */
async function errorCodeOf(response: Response): Promise<string | undefined> {
  try {
    const body = (await response.clone().json()) as {
      error?: { code?: string };
    };
    const code = body?.error?.code;
    return typeof code === "string" && code.trim() ? code : undefined;
  } catch {
    return undefined;
  }
}

async function throwIfUnauthorized(
  path: string,
  response: Response,
  authSnapshot: string,
): Promise<void> {
  if (response.status !== 401 || path.startsWith("/auth/")) {
    return;
  }
  const code = await errorCodeOf(response);
  // Business 401s (peer login, etc.) are not a dead local session.
  if (
    code &&
    !SESSION_UNAUTHORIZED_CODES.has(code) &&
    !path.startsWith("/setup/")
  ) {
    return;
  }
  assertAuthSnapshot(authSnapshot);
  clearAuthToken();
  handleUnauthorized();
  let message = "Unauthorized";
  if (path.startsWith("/setup/")) {
    try {
      const body = (await response.clone().json()) as {
        error?: { message?: string };
      };
      if (body?.error?.message) {
        message = body.error.message;
      }
    } catch {
      /* keep generic message */
    }
  }
  throw new Error(message);
}

export async function request<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  assertNotSetupLocked(path);
  let authSnapshot = getAuthToken();

  const url = getApiUrl(path);

  const headers = buildHeaders(path, options.headers);

  const response = await fetch(url, {
    ...options,
    headers,
  });

  await checkResponseIdentity(authSnapshot, response);
  if (await check503ForSetupRequired(path, response, authSnapshot)) {
    throw new SetupRequiredError();
  }

  await throwIfUnauthorized(path, response, authSnapshot);
  applyRenewedAccessToken(response, authSnapshot);
  authSnapshot = getAuthToken();

  if (!response.ok) {
    if (response.status === 403 && isAgentScopedPath(path)) {
      window.dispatchEvent(
        new CustomEvent(FORBIDDEN_EVENT, { detail: { path } }),
      );
    }
    const text = await response.text().catch(() => "");
    assertAuthSnapshot(authSnapshot);
    throw new Error(
      `Request failed: ${response.status} ${response.statusText}${
        text ? ` - ${text}` : ""
      }`,
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const result = await response.text();
    assertAuthSnapshot(authSnapshot);
    return result as unknown as T;
  }

  const result = await response.json();
  assertAuthSnapshot(authSnapshot);
  return result as T;
}

/**
 * Download a binary resource as a Blob.
 *
 * ``onProgress`` streams the body and reports ``(loaded, total)`` in bytes —
 * used by the PDF preview's progress bar and chat file downloads. ``total`` is
 * 0 when the response has no ``Content-Length`` (e.g. compressed by a proxy).
 */
export async function requestBlob(
  path: string,
  options: RequestInit = {},
  onProgress?: (loaded: number, total: number) => void,
): Promise<Blob> {
  assertNotSetupLocked(path);
  let authSnapshot = getAuthToken();

  const url = getApiUrl(path);
  const headers = buildAuthHeaders(path);
  const response = await fetch(url, {
    ...options,
    headers: { ...headers, ...(options.headers as Record<string, string>) },
  });

  await checkResponseIdentity(authSnapshot, response);
  if (await check503ForSetupRequired(path, response, authSnapshot)) {
    throw new SetupRequiredError();
  }

  await throwIfUnauthorized(path, response, authSnapshot);
  applyRenewedAccessToken(response, authSnapshot);
  authSnapshot = getAuthToken();

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    assertAuthSnapshot(authSnapshot);
    throw new Error(
      `Request failed: ${response.status} ${response.statusText}${
        text ? ` - ${text}` : ""
      }`,
    );
  }

  const total = Number(response.headers.get("content-length")) || 0;
  if (onProgress && response.body) {
    const reader = identityBoundBody(response.body, authSnapshot).getReader();
    const chunks: BlobPart[] = [];
    let loaded = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        chunks.push(value as unknown as BlobPart);
        loaded += value.byteLength;
        onProgress(loaded, total);
      }
    }
    return new Blob(chunks);
  }

  const guarded = response.body
    ? identityBoundBody(response.body, authSnapshot)
    : null;
  const result = await new Response(guarded, {
    headers: response.headers,
  }).blob();
  assertAuthSnapshot(authSnapshot);
  return result;
}

/**
 * Authenticated GET that only checks success — cancels the body without
 * buffering it (existence probes for large workspace files).
 */
export async function probeAuthResource(
  path: string,
  options: RequestInit = {},
): Promise<void> {
  assertNotSetupLocked(path);
  let authSnapshot = getAuthToken();

  const url = getApiUrl(path);
  const headers = buildAuthHeaders(path);
  const response = await fetch(url, {
    ...options,
    headers: { ...headers, ...(options.headers as Record<string, string>) },
  });

  await checkResponseIdentity(authSnapshot, response);
  if (await check503ForSetupRequired(path, response, authSnapshot)) {
    throw new SetupRequiredError();
  }

  await throwIfUnauthorized(path, response, authSnapshot);
  applyRenewedAccessToken(response, authSnapshot);
  authSnapshot = getAuthToken();

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    assertAuthSnapshot(authSnapshot);
    throw new Error(
      `Request failed: ${response.status} ${response.statusText}${
        text ? ` - ${text}` : ""
      }`,
    );
  }

  try {
    await response.body?.cancel();
  } catch {
    /* ignore cancel failures */
  }
}

/**
 * POST JSON and hand back the response body as a byte stream (chunked TTS).
 * Mirrors requestBlob()'s auth/setup/401 handling but never buffers.
 */
export async function requestStream(
  path: string,
  options: RequestInit = {},
): Promise<{ contentType: string; body: ReadableStream<Uint8Array> }> {
  assertNotSetupLocked(path);
  let authSnapshot = getAuthToken();

  const url = getApiUrl(path);
  const headers = buildAuthHeaders(path);
  const response = await fetch(url, {
    ...options,
    headers: { ...headers, ...(options.headers as Record<string, string>) },
  });

  await checkResponseIdentity(authSnapshot, response);
  if (await check503ForSetupRequired(path, response, authSnapshot)) {
    throw new SetupRequiredError();
  }

  await throwIfUnauthorized(path, response, authSnapshot);
  applyRenewedAccessToken(response, authSnapshot);
  authSnapshot = getAuthToken();

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    assertAuthSnapshot(authSnapshot);
    throw new Error(
      `Request failed: ${response.status} ${response.statusText}${
        text ? ` - ${text}` : ""
      }`,
    );
  }

  if (!response.body) {
    throw new Error("Empty stream from server");
  }

  return {
    contentType: response.headers.get("content-type") || "",
    body: identityBoundBody(response.body, authSnapshot),
  };
}

export type UploadProgressHandler = (percent: number) => void;

/**
 * Upload a FormData payload (no explicit Content-Type — browser handles boundary).
 * Uses XMLHttpRequest so callers can report upload progress.
 */
export async function requestUpload<T = unknown>(
  path: string,
  body: FormData | Blob,
  options: RequestInit = {},
  onProgress?: UploadProgressHandler,
): Promise<T> {
  assertNotSetupLocked(path);
  let authSnapshot = getAuthToken();

  const url = getApiUrl(path);
  const headers = buildAuthHeaders(path);
  const method = options.method ?? "POST";

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url);

    for (const [key, value] of Object.entries(headers)) {
      xhr.setRequestHeader(key, value);
    }
    if (options.headers) {
      const extraEntries =
        options.headers instanceof Headers
          ? Array.from(options.headers.entries())
          : Array.isArray(options.headers)
          ? options.headers
          : Object.entries(options.headers);
      for (const [k, v] of extraEntries) {
        xhr.setRequestHeader(k, String(v));
      }
    }

    xhr.upload.onprogress = (event) => {
      try {
        assertAuthSnapshot(authSnapshot);
      } catch {
        xhr.abort();
        return;
      }
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    if (options.signal) {
      if (options.signal.aborted) {
        reject(new DOMException("The operation was aborted.", "AbortError"));
        return;
      }
      options.signal.addEventListener(
        "abort",
        () => {
          xhr.abort();
        },
        { once: true },
      );
    }

    xhr.onload = () => {
      void (async () => {
        assertAuthSnapshot(authSnapshot);
        const status = xhr.status;
        const responseText = xhr.responseText;
        const responseHeaders = new Headers();
        const renewed = xhr.getResponseHeader(ACCESS_TOKEN_RESPONSE_HEADER);
        if (renewed) {
          responseHeaders.set(ACCESS_TOKEN_RESPONSE_HEADER, renewed);
        }
        const response = new Response(responseText, {
          status,
          headers: responseHeaders,
        });

        await checkResponseIdentity(authSnapshot, response);
        if (await check503ForSetupRequired(path, response, authSnapshot)) {
          reject(new SetupRequiredError());
          return;
        }

        try {
          await throwIfUnauthorized(path, response, authSnapshot);
        } catch (err) {
          reject(err);
          return;
        }

        applyRenewedAccessToken(response, authSnapshot);
        authSnapshot = getAuthToken();

        if (!response.ok) {
          // Same shape as request() so parseApiError() can read the error envelope.
          reject(
            new Error(
              `Upload failed: ${status}${
                responseText ? ` - ${responseText}` : ""
              }`,
            ),
          );
          return;
        }

        let result: unknown;
        try {
          result = await response.json();
        } catch {
          reject(new Error("Upload failed: invalid JSON response"));
          return;
        }
        assertAuthSnapshot(authSnapshot);
        resolve(result as T);
      })().catch(reject);
    };

    xhr.onerror = () => reject(new Error("Network error"));
    xhr.onabort = () =>
      reject(new DOMException("The operation was aborted.", "AbortError"));

    xhr.send(body);
  });
}
