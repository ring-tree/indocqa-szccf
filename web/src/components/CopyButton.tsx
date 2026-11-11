import { useEffect, useRef, useState } from "react";
import { markdownToPlainText } from "../lib/text";

type CopyMode = "markdown" | "plain";

function fallbackCopy(text: string) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } finally {
    area.remove();
  }
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
      <rect x="8" y="8" width="11" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
      <path d="m5 13 4 4L19 7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function CopyButton({ content, plainText }: { content: string; plainText?: string }) {
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState<CopyMode | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!feedback) return;
    const timer = window.setTimeout(() => setFeedback(null), 1800);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  const copy = async (mode: CopyMode) => {
    const text = mode === "markdown" ? content : (plainText ?? markdownToPlainText(content));
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        fallbackCopy(text);
      }
      setFeedback(mode);
    } catch {
      fallbackCopy(text);
      setFeedback(mode);
    }
    setOpen(false);
  };

  return (
    <div className="ui-copy" ref={wrapRef}>
      {feedback && (
        <span className="ui-copy-toast" role="status" aria-live="polite">
          <CheckIcon />
          {feedback === "markdown" ? "已复制 Markdown" : "已复制纯文本"}
        </span>
      )}
      <button
        type="button"
        className="ui-copy-btn"
        aria-label="复制回答"
        aria-haspopup="menu"
        aria-expanded={open}
        title="复制回答"
        onClick={() => setOpen((value) => !value)}
      >
        {feedback ? <CheckIcon /> : <CopyIcon />}
      </button>
      {open && (
        <div className="ui-copy-menu" role="menu" aria-label="复制格式">
          <button type="button" role="menuitem" onClick={() => copy("markdown")}>
            复制为 Markdown
          </button>
          <button type="button" role="menuitem" onClick={() => copy("plain")}>
            复制为 TXT
          </button>
        </div>
      )}
    </div>
  );
}
