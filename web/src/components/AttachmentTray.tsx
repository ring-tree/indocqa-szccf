import { useEffect, useState } from "react";
import { fileTypeLabel, formatSize, kindOfFile } from "../lib/attachments";
import type { PendingAttachment } from "../lib/types";

interface Props {
  items: PendingAttachment[];
  onRetry: (localId: string) => void;
  onRemove: (localId: string) => void;
}

function ImageThumb({ file }: { file: File }) {
  const [url, setUrl] = useState("");

  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);

  return url ? <img src={url} alt={file.name} className="ui-attachment-thumb" /> : null;
}

function StatusText({ item }: { item: PendingAttachment }) {
  if (item.status === "ready") return <span className="ui-attachment-ready">已就绪</span>;
  if (item.status === "error") {
    return <span className="ui-attachment-error">{item.error || "上传失败"}</span>;
  }
  if (item.progress >= 100) return <span>解析中…</span>;
  return <span>上传中 {Math.round(item.progress)}%</span>;
}

export default function AttachmentTray({ items, onRetry, onRemove }: Props) {
  if (items.length === 0) return null;
  const allReady = items.every((item) => item.status === "ready");
  return (
    <div className="ui-attachment-tray" aria-live="polite">
      <div className="ui-attachment-tray-head">
        <span>待发送附件（{items.length}）</span>
        <span className={allReady ? "ui-attachment-ready" : "text-[var(--muted)]"}>
          {allReady ? "可以发送" : "上传完成后可发送"}
        </span>
      </div>
      {items.map((item) => {
        const kind = kindOfFile(item.file);
        return (
          <div key={item.localId} className="ui-attachment-item">
            {kind === "image" ? (
              <ImageThumb file={item.file} />
            ) : (
              <span className="ui-attachment-file-icon" aria-hidden="true">
                {fileTypeLabel(item.file.name)}
              </span>
            )}
            <div className="ui-attachment-meta">
              <div className="ui-attachment-name" title={item.file.name}>
                {item.file.name}
              </div>
              <div className="ui-attachment-sub">
                <span>{formatSize(item.file.size)}</span>
                <StatusText item={item} />
              </div>
              {item.status === "uploading" && (
                <div
                  className="ui-progress"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={Math.round(item.progress)}
                  aria-label={`${item.file.name} 上传进度`}
                >
                  <div
                    className="ui-progress-bar"
                    style={{ width: `${Math.min(100, Math.max(0, item.progress))}%` }}
                  />
                </div>
              )}
            </div>
            <div className="ui-attachment-actions">
              {item.status === "error" && (
                <button
                  type="button"
                  className="ui-btn ui-btn-sm ui-btn-ghost"
                  onClick={() => onRetry(item.localId)}
                >
                  重试
                </button>
              )}
              <button
                type="button"
                className="ui-btn ui-btn-sm ui-btn-ghost"
                onClick={() => onRemove(item.localId)}
                aria-label={`移除 ${item.file.name}`}
              >
                移除
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
