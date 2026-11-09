import { request, requestUpload, getAuthStorageIdentity } from "../request";

export interface UploadResponse {
  path: string;
  workspace_path: string;
  filename: string;
  media_type: string;
  url: string;
  access_url: string;
}

interface ChunkedUploadStatus {
  upload_id: string;
  size: number;
  chunk_size: number;
  chunk_count: number;
  received: number[];
}

/** Bytes uploaded so far for one file. */
export type UploadBytesHandler = (loaded: number) => void;

/**
 * Files above this size go up in chunks so each request stays below proxy
 * body limits (Cloudflare: 100 MB). The server picks the chunk size.
 */
export const CHUNKED_UPLOAD_THRESHOLD = 32 * 1024 * 1024;

/**
 * Video and audio always go up in chunks, however small: a one-request upload
 * has no retry, so a short network drop would fail the file outright.
 * The extension covers files the browser gives no or a generic type.
 */
const MEDIA_EXTENSION =
  /\.(mp4|m4v|mov|mkv|webm|avi|flv|wmv|ts|mts|3gp|mp3|m4a|aac|wav|flac|ogg|opus)$/i;

function sendsInChunks(file: File): boolean {
  if (file.size > CHUNKED_UPLOAD_THRESHOLD) return true;
  return (
    file.size > 0 &&
    (/^(video|audio)\//.test(file.type) || MEDIA_EXTENSION.test(file.name))
  );
}

/** Backoff 1 + 2 + 4 + 8 + 16 s: a chunk rides out about half a minute offline. */
const CHUNK_RETRIES = 5;
/** Chunk requests in flight per file. */
const CHUNK_CONCURRENCY = 6;

/**
 * Unfinished chunked uploads are remembered per browser so picking the same
 * file again (e.g. after a page reload) resumes instead of starting over.
 * Like the server, a session is forgotten 24 hours after its last chunk.
 * Desktop-app drops get a fresh ``lastModified`` each time, so they don't resume.
 */
const RESUME_PREFIX = "octop.chunkedUpload:";
const RESUME_TTL_MS = 24 * 60 * 60 * 1000;

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** The server no longer has this session (expired, cancelled, another user's). */
function isGone(err: unknown): boolean {
  return err instanceof Error && /^Request failed: 40[34]\b/.test(err.message);
}

function resumeKey(agentId: string, file: File): string {
  const identity = getAuthStorageIdentity();
  const scope = identity ? `identity:${encodeURIComponent(identity)}|` : "";
  return `${RESUME_PREFIX}${scope}${agentId}|${file.name}|${file.size}|${file.lastModified}`;
}

/** Drop remembered sessions the server has already expired. */
function pruneResumeIds(now: number): void {
  const identity = getAuthStorageIdentity();
  const prefix = identity
    ? `${RESUME_PREFIX}identity:${encodeURIComponent(identity)}|`
    : RESUME_PREFIX;
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const key = localStorage.key(i);
      if (!key?.startsWith(prefix)) continue;
      if (!identity && key.startsWith(`${RESUME_PREFIX}identity:`)) continue;
      const at = Number(JSON.parse(localStorage.getItem(key) ?? "{}").at);
      if (!(now - at < RESUME_TTL_MS)) localStorage.removeItem(key);
    }
  } catch {
    // Storage unavailable (private mode, blocked site data): no resume.
  }
}

function loadResumeId(key: string): string | null {
  try {
    const id = JSON.parse(localStorage.getItem(key) ?? "{}").id;
    return typeof id === "string" ? id : null;
  } catch {
    return null;
  }
}

function saveResumeId(key: string, id: string): void {
  try {
    localStorage.setItem(key, JSON.stringify({ id, at: Date.now() }));
  } catch {
    // Storage unavailable: the upload still works, it just can't resume.
  }
}

function clearResumeId(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

/** Reuse this file's unfinished session when the server still has it. */
async function openSession(
  base: string,
  file: File,
  key: string,
): Promise<ChunkedUploadStatus> {
  pruneResumeIds(Date.now());
  const previous = loadResumeId(key);
  if (previous) {
    let status: ChunkedUploadStatus | null = null;
    try {
      status = await request<ChunkedUploadStatus>(`${base}/${previous}`);
    } catch (err) {
      // A network error or 5xx keeps the id so a later attempt can resume.
      if (!isGone(err)) throw err;
    }
    if (status?.size === file.size) return status;
    clearResumeId(key);
  }
  const session = await request<ChunkedUploadStatus>(base, {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      size: file.size,
      content_type: file.type || null,
    }),
  });
  saveResumeId(key, session.upload_id);
  return session;
}

async function uploadChunked(
  agentId: string,
  file: File,
  onBytes?: UploadBytesHandler,
): Promise<UploadResponse> {
  const base = `/agents/${agentId}/upload/chunked`;
  const key = resumeKey(agentId, file);
  const session = await openSession(base, file, key);
  const url = `${base}/${session.upload_id}`;
  const chunkBytes = (index: number) =>
    Math.min(session.chunk_size, file.size - index * session.chunk_size);
  let done = 0;
  // Set once a chunk gives up, so the other workers stop retrying.
  let failed = false;
  const inflight = new Map<number, number>();
  const report = () => {
    let loaded = done;
    for (const bytes of inflight.values()) loaded += bytes;
    onBytes?.(loaded);
  };

  const sendChunk = async (index: number) => {
    const start = index * session.chunk_size;
    const blob = file.slice(start, start + chunkBytes(index));
    for (let attempt = 0; ; attempt++) {
      try {
        await requestUpload(
          `${url}/chunks/${index}`,
          blob,
          {
            method: "PUT",
            headers: { "Content-Type": "application/octet-stream" },
          },
          (percent) => {
            inflight.set(index, (blob.size * percent) / 100);
            report();
          },
        );
        inflight.delete(index);
        done += blob.size;
        report();
        saveResumeId(key, session.upload_id);
        return;
      } catch (err) {
        inflight.delete(index);
        report();
        if (isAbort(err) || failed || attempt >= CHUNK_RETRIES) throw err;
        await sleep(1000 * 2 ** attempt);
        if (failed) throw err;
      }
    }
  };

  const sendMissing = async (received: number[]) => {
    const have = new Set(received);
    done = received.reduce((sum, i) => sum + chunkBytes(i), 0);
    report();
    const queue: number[] = [];
    for (let index = 0; index < session.chunk_count; index++) {
      if (!have.has(index)) queue.push(index);
    }
    failed = false;
    const worker = async () => {
      while (queue.length > 0 && !failed) {
        try {
          await sendChunk(queue.shift()!);
        } catch (err) {
          failed = true;
          throw err;
        }
      }
    };
    const workers = Math.min(CHUNK_CONCURRENCY, queue.length);
    // Wait for every worker before failing, so nothing keeps uploading (or
    // reporting progress) after the caller has been told the upload failed.
    const results = await Promise.allSettled(
      Array.from({ length: workers }, worker),
    );
    for (const result of results) {
      if (result.status === "rejected") throw result.reason;
    }
  };

  const complete = () =>
    request<UploadResponse>(`${url}/complete`, { method: "POST" });

  // On failure the session is kept (not cancelled): picking the same file
  // again resumes it, and a complete whose reply was lost returns its result.
  await sendMissing(session.received);
  let result: UploadResponse;
  try {
    result = await complete();
  } catch {
    // A chunk (or the reply) was lost; resend whatever the server is missing once.
    const status = await request<ChunkedUploadStatus>(url);
    await sendMissing(status.received);
    result = await complete();
  }
  clearResumeId(key);
  return result;
}

/**
 * Upload a chat attachment into the agent workspace ``inbound/`` directory.
 * Large files are sent in chunks; ``onBytes`` reports bytes sent so far.
 */
export async function uploadFile(
  agentId: string,
  file: File,
  onBytes?: UploadBytesHandler,
): Promise<UploadResponse> {
  if (sendsInChunks(file)) {
    return uploadChunked(agentId, file, onBytes);
  }
  const formData = new FormData();
  formData.append("file", file);
  return requestUpload<UploadResponse>(
    `/agents/${agentId}/upload`,
    formData,
    {},
    onBytes ? (percent) => onBytes((file.size * percent) / 100) : undefined,
  );
}

export const uploadImage = uploadFile;

export const uploadApi = { uploadFile, uploadImage };
