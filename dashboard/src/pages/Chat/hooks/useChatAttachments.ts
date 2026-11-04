import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, ClipboardEvent, DragEvent } from "react";
import { useTranslation } from "react-i18next";
import { uploadFile } from "../../../api/modules/upload";
import { agentAttachmentAccessUrl } from "../../../utils/toolMediaBlocks";
import type { ChatAttachment } from "./useChat";
import { message as antMessage } from "@/utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";

import { inferAttachmentKind } from "../utils/chatAttachments";
import {
  DESKTOP_FILE_DROP_EVENT,
  filesFromDesktopDrop,
  type DesktopFileDropDetail,
} from "../../../utils/desktopFileDrop";
import {
  dragHasFiles,
  filesFromSnapshot,
  MAX_CHAT_UPLOAD_FILES,
  snapshotDroppedEntries,
  type CollectedDrop,
} from "../utils/droppedFiles";
import { useServerUploadLimit } from "../../../hooks/useServerUploadLimit";

/** Files uploading at once; the rest wait their turn. */
const FILES_IN_FLIGHT = 2;

export function useChatAttachments(agentId: string | null | undefined) {
  const { t } = useTranslation();
  const { maxUploadBytes, maxUploadMb } = useServerUploadLimit();
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  const processFiles = useCallback(
    async (files: FileList | File[]) => {
      const incoming = Array.from(files);
      const capped =
        incoming.length > MAX_CHAT_UPLOAD_FILES
          ? incoming.slice(0, MAX_CHAT_UPLOAD_FILES)
          : incoming;
      if (incoming.length > MAX_CHAT_UPLOAD_FILES) {
        antMessage.warning(
          t(
            "upload.tooMany",
            "Only the first {{max}} files can be uploaded at once",
            { max: MAX_CHAT_UPLOAD_FILES },
          ),
        );
      }
      const fileArr = capped.filter((f) => {
        if (f.size > maxUploadBytes) {
          antMessage.error(
            t("upload.tooLarge", "File too large (max {{maxMb}}MB): {{name}}", {
              name: f.name,
              maxMb: maxUploadMb,
            }),
          );
          return false;
        }
        return true;
      });

      if (fileArr.length === 0) return;

      if (!agentId) {
        antMessage.error(t("upload.failed", "Upload failed"));
        return;
      }

      setUploading(true);
      const totalBytes = fileArr.reduce((sum, f) => sum + f.size, 0);
      const loaded = fileArr.map(() => 0);
      const reportProgress = () => {
        if (totalBytes <= 0) return;
        const sent = loaded.reduce((sum, n) => sum + n, 0);
        setUploadProgress(Math.min(100, Math.floor((sent / totalBytes) * 100)));
      };
      setUploadProgress(totalBytes > 0 ? 0 : null);
      const uploadOne = async (file: File, i: number) => {
        const res = await uploadFile(agentId, file, (bytes) => {
          loaded[i] = bytes;
          reportProgress();
        });
        const workspacePath = res.path || res.workspace_path;
        const previewUrl =
          res.access_url ||
          res.url ||
          (workspacePath
            ? agentAttachmentAccessUrl(agentId, workspacePath, res.media_type)
            : "");
        return {
          url: previewUrl,
          filename: res.filename,
          mediaType: res.media_type,
          workspacePath,
          kind: inferAttachmentKind(file, res.media_type),
        } satisfies ChatAttachment;
      };
      try {
        // A few files at a time, so a dropped connection loses less progress.
        const settled: PromiseSettledResult<ChatAttachment>[] = [];
        let next = 0;
        const worker = async () => {
          while (next < fileArr.length) {
            const i = next++;
            try {
              settled[i] = {
                status: "fulfilled",
                value: await uploadOne(fileArr[i], i),
              };
            } catch (reason: unknown) {
              settled[i] = { status: "rejected", reason };
            }
          }
        };
        await Promise.all(
          Array.from(
            { length: Math.min(FILES_IN_FLIGHT, fileArr.length) },
            worker,
          ),
        );
        // Keep the files that made it; name only the ones that didn't.
        const done: ChatAttachment[] = [];
        const failed: string[] = [];
        let firstError: unknown;
        settled.forEach((result, i) => {
          if (result.status === "fulfilled") {
            done.push(result.value);
          } else {
            failed.push(fileArr[i].name);
            firstError ??= result.reason;
          }
        });
        if (done.length > 0) setAttachments((prev) => [...prev, ...done]);
        if (failed.length > 0 && done.length === 0) {
          antMessage.error(
            apiErrorMessage(firstError, t("upload.failed", "Upload failed"), t),
          );
        } else if (failed.length > 0) {
          antMessage.error(
            t(
              "upload.failedSome",
              "{{count}} of {{total}} files failed to upload: {{names}}. Pick them again to retry.",
              {
                count: failed.length,
                total: fileArr.length,
                names: failed.join(", "),
              },
            ),
          );
        }
      } finally {
        setUploading(false);
        setUploadProgress(null);
      }
    },
    [agentId, maxUploadBytes, maxUploadMb, t],
  );

  const handleFileSelect = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const handleFolderSelect = useCallback(() => {
    const input = folderInputRef.current;
    if (!input) return;
    input.setAttribute("webkitdirectory", "");
    input.click();
  }, []);

  const handleFileChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      if (e.target.files && e.target.files.length > 0) {
        void processFiles(e.target.files);
      }
      e.target.value = "";
    },
    [processFiles],
  );

  const removeAttachment = useCallback((index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const clearAttachments = useCallback(() => {
    setAttachments([]);
  }, []);

  const restoreAttachments = useCallback((next: ChatAttachment[]) => {
    setAttachments(next.map((a) => ({ ...a })));
  }, []);

  const handlePaste = useCallback(
    (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const pastedFiles: File[] = [];
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (file) pastedFiles.push(file);
        }
      }
      if (pastedFiles.length > 0) {
        e.preventDefault();
        void processFiles(pastedFiles);
      }
    },
    [processFiles],
  );

  const handleDragEnter = useCallback((e: DragEvent) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: DragEvent<HTMLElement>) => {
    const next = e.relatedTarget;
    if (next instanceof Node && e.currentTarget.contains(next)) return;
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
  }, []);

  const handleDragOver = useCallback((e: DragEvent) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "copy";
    setDragOver(true);
  }, []);

  const handleDrop = useCallback(
    (e: DragEvent) => {
      const snapshot = snapshotDroppedEntries(e.dataTransfer);
      if (!snapshot.hadFileItems) return;
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);
      return (async () => {
        let collected: CollectedDrop;
        try {
          collected = await filesFromSnapshot(snapshot);
        } catch {
          antMessage.error(t("upload.failed", "Upload failed"));
          return;
        }
        if (collected.truncated) {
          antMessage.warning(
            t(
              "upload.tooMany",
              "Only the first {{max}} files can be uploaded at once",
              { max: MAX_CHAT_UPLOAD_FILES },
            ),
          );
        }
        if (collected.files.length === 0) {
          antMessage.warning(t("upload.emptyDrop", "No files to upload"));
          return;
        }
        await processFiles(collected.files);
      })();
    },
    [processFiles, t],
  );

  useEffect(() => {
    const onDesktopDrop = (event: Event) => {
      const detail = (event as CustomEvent<DesktopFileDropDetail>).detail;
      setDragOver(false);
      void (async () => {
        if (detail?.truncated) {
          antMessage.warning(
            t(
              "upload.tooMany",
              "Only the first {{max}} files can be uploaded at once",
              { max: detail.max ?? 500 },
            ),
          );
        }
        try {
          const files = await filesFromDesktopDrop(detail);
          if (files.length === 0) {
            antMessage.warning(t("upload.emptyDrop", "No files to upload"));
            return;
          }
          await processFiles(files);
        } catch {
          antMessage.error(t("upload.failed", "Upload failed"));
        }
      })();
    };
    window.addEventListener(DESKTOP_FILE_DROP_EVENT, onDesktopDrop);
    return () =>
      window.removeEventListener(DESKTOP_FILE_DROP_EVENT, onDesktopDrop);
  }, [processFiles, t]);

  return {
    attachments,
    uploading,
    uploadProgress,
    dragOver,
    fileInputRef,
    folderInputRef,
    processFiles,
    handleFileSelect,
    handleFolderSelect,
    handleFileChange,
    removeAttachment,
    clearAttachments,
    restoreAttachments,
    handlePaste,
    handleDragEnter,
    handleDragLeave,
    handleDragOver,
    handleDrop,
  };
}
