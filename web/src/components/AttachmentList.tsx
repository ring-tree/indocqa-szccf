import { fileTypeLabel, formatSize } from "../lib/attachments";
import type { AttachmentInfo } from "../lib/types";

interface Props {
  attachments: AttachmentInfo[];
}

function AttachmentLabel({ attachment }: { attachment: AttachmentInfo }) {
  return (
    <span className="ui-attachment-caption">
      <span className="ui-attachment-caption-name">{attachment.filename}</span>
      <span>{formatSize(attachment.size)}</span>
      {attachment.parseStatus === "metadata_only" && (
        <span className="ui-attachment-chip">未提取文本</span>
      )}
    </span>
  );
}

export default function AttachmentList({ attachments }: Props) {
  if (attachments.length === 0) return null;
  const images = attachments.filter((item) => item.kind === "image");
  const others = attachments.filter((item) => item.kind !== "image");

  return (
    <div className="ui-attachment-list">
      {images.length > 0 && (
        <div className={`ui-attachment-grid ${images.length === 1 ? "is-single" : ""}`}>
          {images.map((item) => (
            <a
              key={item.id}
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="ui-attachment-image"
              title={`预览 ${item.filename}`}
            >
              <img src={item.url} alt={item.filename} loading="lazy" />
            </a>
          ))}
        </div>
      )}
      {others.map((item) => {
        if (item.kind === "audio") {
          return (
            <div key={item.id} className="ui-attachment-media">
              <div className="ui-attachment-media-head">
                <span className="ui-attachment-doc-icon" aria-hidden="true">
                  {fileTypeLabel(item.filename)}
                </span>
                <AttachmentLabel attachment={item} />
              </div>
              <audio controls preload="metadata" src={item.url} />
            </div>
          );
        }
        if (item.kind === "video") {
          return (
            <div key={item.id} className="ui-attachment-media">
              <video controls playsInline preload="metadata" src={item.url} />
              <AttachmentLabel attachment={item} />
            </div>
          );
        }
        const href = item.mime === "application/pdf" ? item.url : item.downloadUrl ?? item.url;
        return (
          <a
            key={item.id}
            href={href}
            target="_blank"
            rel="noreferrer"
            className="ui-attachment-doc"
            title={`打开 ${item.filename}`}
          >
            <span className="ui-attachment-doc-icon" aria-hidden="true">
              {fileTypeLabel(item.filename)}
            </span>
            <span className="ui-attachment-doc-meta">
              <span className="ui-attachment-name">{item.filename}</span>
              <span className="ui-attachment-sub">{formatSize(item.size)} · 点击预览/下载</span>
            </span>
            {item.parseStatus === "metadata_only" && (
              <span className="ui-attachment-chip">未提取文本</span>
            )}
          </a>
        );
      })}
    </div>
  );
}
