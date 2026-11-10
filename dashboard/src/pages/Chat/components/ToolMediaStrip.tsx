import { Image } from "antd";
import { useTranslation } from "react-i18next";
import { useAuthImageSrc } from "../../../hooks/useAuthImageSrc";
import type { ToolMediaItem } from "../../../utils/toolMediaBlocks";
import { MessageFileCard } from "./MessageFileCard";
import styles from "../index.module.less";

function RefreshableImage({
  url,
  filename,
  idx,
}: {
  url: string;
  filename?: string;
  idx: number;
}) {
  const { t } = useTranslation();
  const { src, loadState } = useAuthImageSrc(url, filename);

  if (loadState === "error") {
    return (
      <span className={styles.toolMediaFallback}>
        {filename || t("chat.imageLoadFailed")}
      </span>
    );
  }

  if (loadState === "loading" || !src) {
    return <span className={styles.toolMediaFallback}>…</span>;
  }

  return (
    <Image
      src={src}
      alt={filename || `image-${idx}`}
      className={styles.toolMediaImage}
    />
  );
}

export function ToolMediaStrip({
  images,
  videos,
  files = [],
  agentId,
}: {
  images: ToolMediaItem[];
  videos: ToolMediaItem[];
  files?: Array<{ url: string; filename?: string }>;
  agentId?: string | null;
}) {
  if (images.length === 0 && videos.length === 0 && files.length === 0) {
    return null;
  }

  return (
    <div className={styles.toolMediaStrip}>
      {images.length > 0 && (
        <div className={styles.messageImages}>
          <Image.PreviewGroup>
            {images.map((img, idx) => (
              <RefreshableImage
                key={`${img.url}-${idx}`}
                url={img.url}
                filename={img.filename}
                idx={idx}
              />
            ))}
          </Image.PreviewGroup>
        </div>
      )}
      {/* Download card only: an inline player would pull the whole file as
          soon as the chat opens, which on a slow link looks stuck and slows
          the user's own downloads. */}
      {videos.length > 0 && (
        <div className={styles.messageFiles}>
          {videos.map((video, idx) => (
            <MessageFileCard
              key={`${video.url}-${idx}`}
              url={video.url}
              filename={video.filename || "video"}
              agentId={agentId}
            />
          ))}
        </div>
      )}
      {files.length > 0 && (
        <div className={styles.messageFiles}>
          {files.map((file, idx) => (
            <MessageFileCard
              key={`${file.url}-${idx}`}
              url={file.url}
              filename={file.filename}
              agentId={agentId}
            />
          ))}
        </div>
      )}
    </div>
  );
}
