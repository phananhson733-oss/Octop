import { request, requestUpload } from "../request";

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

const CHUNK_RETRIES = 3;

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function uploadChunked(
  agentId: string,
  file: File,
  onBytes?: UploadBytesHandler,
): Promise<UploadResponse> {
  const base = `/agents/${agentId}/upload/chunked`;
  const session = await request<ChunkedUploadStatus>(base, {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      size: file.size,
      content_type: file.type || null,
    }),
  });
  const url = `${base}/${session.upload_id}`;
  const chunkBytes = (index: number) =>
    Math.min(session.chunk_size, file.size - index * session.chunk_size);
  let done = 0;

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
          (percent) => onBytes?.(done + (blob.size * percent) / 100),
        );
        done += blob.size;
        onBytes?.(done);
        return;
      } catch (err) {
        if (isAbort(err) || attempt >= CHUNK_RETRIES) throw err;
        onBytes?.(done);
        await sleep(1000 * 2 ** attempt);
      }
    }
  };

  const sendMissing = async (received: number[]) => {
    const have = new Set(received);
    for (let index = 0; index < session.chunk_count; index++) {
      if (!have.has(index)) await sendChunk(index);
    }
  };

  try {
    await sendMissing(session.received);
    try {
      return await request<UploadResponse>(`${url}/complete`, {
        method: "POST",
      });
    } catch {
      // A chunk may have been lost; resend whatever the server is missing once.
      const status = await request<ChunkedUploadStatus>(url);
      done = status.received.reduce((sum, i) => sum + chunkBytes(i), 0);
      await sendMissing(status.received);
      return await request<UploadResponse>(`${url}/complete`, {
        method: "POST",
      });
    }
  } catch (err) {
    void request(url, { method: "DELETE" }).catch(() => undefined);
    throw err;
  }
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
  if (file.size > CHUNKED_UPLOAD_THRESHOLD) {
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
