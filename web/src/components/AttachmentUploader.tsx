import { useRef } from "react";
import { ACCEPT_ATTR, MAX_FILES } from "../lib/attachments";

interface Props {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}

/** 聊天输入区的附件入口：点击选择；拖拽由 ChatView 的 composer 区域处理。 */
export default function AttachmentUploader({ onFiles, disabled = false }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT_ATTR}
        className="hidden"
        data-testid="attachment-input"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          if (files.length > 0) onFiles(files);
          event.target.value = "";
        }}
      />
      <button
        type="button"
        className="ui-btn ui-btn-sm ui-btn-ghost ui-attachment-trigger"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
        aria-label={`添加附件，最多 ${MAX_FILES} 个`}
        title="添加图片、文档、音频或视频"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M21.4 11.6 12.6 20.4a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 1 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5" />
        </svg>
        <span className="hidden sm:inline">附件</span>
      </button>
    </>
  );
}
