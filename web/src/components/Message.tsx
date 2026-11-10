import { memo, useCallback, useState } from "react";
import Markdown from "./Markdown";
import type { ChatMessage, Source, TraceStep } from "../lib/types";

const CONF_TEXT: Record<string, string> = {
  high: "置信度 高",
  mid: "置信度 中",
  low: "置信度 低",
};

const BOUNDARY_TEXT: Record<string, string> = {
  general: "非知识库内容",
  refused: "资料未覆盖",
};

const STAGE_LABEL: Record<string, string> = {
  classify: "意图分类",
  retrieve: "检索",
  decide: "多跳决策",
  citation_retry: "引用补正",
  boundary: "知识边界",
};

interface Props {
  message: ChatMessage;
  anchor: string;
  onRetry?: () => void;
}

function Message({ message, anchor, onRetry }: Props) {
  const [activeSource, setActiveSource] = useState<number | null>(null);

  const jumpToSource = useCallback(
    (index: number) => {
      if (activeSource === index) {
        setActiveSource(null);
        return;
      }
      setActiveSource(index);
      const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      document.getElementById(`${anchor}-source-${index}`)?.scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "center",
      });
    },
    [activeSource, anchor],
  );

  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="ui-message-user max-w-[85%] whitespace-pre-wrap break-words px-5 py-3 text-sm lg:max-w-[60ch]">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="ui-message-assistant max-w-[92%] break-words px-5 py-4 text-sm leading-6 lg:max-w-[72ch]">
        {message.content ? (
          <Markdown onCite={jumpToSource}>{message.content}</Markdown>
        ) : (
          <span className="text-[var(--muted)]">{message.streaming ? "正在检索…" : ""}</span>
        )}
        {message.streaming && message.content && (
          <span
            className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-[var(--brand)] align-middle"
            aria-hidden="true"
          />
        )}
        {message.error && (
          <div role="alert" className="mt-3 flex items-center gap-3 text-xs text-[var(--danger)]">
            <span>请求失败，请检查后端或网络。</span>
            {onRetry && (
              <button type="button" onClick={onRetry} className="ui-btn ui-btn-sm ui-btn-danger">
                重试
              </button>
            )}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 lg:max-w-[72ch]">
        {message.confidence && (
          <span
            className="ui-chip"
            data-tone={message.confidence}
            title={message.confidenceReason}
          >
            {CONF_TEXT[message.confidence] ?? message.confidence}
          </span>
        )}
        {message.boundary && (
          <span className="ui-chip" data-tone={message.boundary}>
            {BOUNDARY_TEXT[message.boundary] ?? message.boundary}
          </span>
        )}
        {message.stopped && <span className="ui-chip">已停止</span>}
        {message.model && (
          <span className="ui-chip" title="实际使用的输出模型">
            模型 {message.model}
          </span>
        )}
        {message.trace && message.trace.length > 0 && <TracePanel steps={message.trace} />}
      </div>

      {message.sources && message.sources.length > 0 && (
        <SourcePanel
          sources={message.sources}
          anchor={anchor}
          active={activeSource}
          onSelect={jumpToSource}
        />
      )}
    </div>
  );
}

function TracePanel({ steps }: { steps: TraceStep[] }) {
  return (
    <details className="ui-trace px-2 py-1 text-xs">
      <summary className="cursor-pointer select-none rounded">
        执行链（{steps.length} 步）
      </summary>
      <ul className="mt-1.5 space-y-1">
        {steps.map((s, i) => (
          <li key={i} className="flex gap-2">
            <span className="w-16 shrink-0 text-[var(--muted)]">
              {STAGE_LABEL[s.stage] ?? s.stage}
            </span>
            <span className="min-w-0 break-words">
              {s.stage === "classify" && `${s.intent} → ${s.plan}`}
              {s.stage === "retrieve" &&
                `${s.strategy}${s.hop ? ` · 第${s.hop}跳` : ""} · 命中 ${s.hits} 条${
                  s.expansions && s.expansions.length
                    ? ` · 术语扩展: ${s.expansions.join("; ")}`
                    : ""
                }`}
              {(s.stage === "decide" || s.stage === "citation_retry" || s.stage === "boundary") &&
                s.result}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function SourcePanel({
  sources,
  anchor,
  active,
  onSelect,
}: {
  sources: Source[];
  anchor: string;
  active: number | null;
  onSelect: (index: number) => void;
}) {
  return (
    <div className="ui-source-panel p-3 lg:max-w-[72ch]">
      <div className="mb-2 text-xs font-bold text-[var(--muted)]">
        引用来源（{sources.length}）
      </div>
      <ul className="space-y-1.5">
        {sources.map((s) => (
          <li key={s.index} id={`${anchor}-source-${s.index}`}>
            <button
              type="button"
              onClick={() => onSelect(s.index)}
              aria-current={active === s.index ? "true" : undefined}
              aria-label={`查看来源 ${s.index}：${s.docName} ${s.section}`}
              className={`ui-source-item text-xs ${active === s.index ? "is-active" : ""}`}
            >
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-[var(--line)] text-[10px] font-bold">
                {s.index}
              </span>
              <span className="min-w-0 truncate">
                {s.folder && <span className="ui-source-folder">{s.folder}</span>}
                {s.docName} · {s.section}
                {s.page != null && ` · 第${s.page}页`}
                {s.locator && ` · ${s.locator}`}
              </span>
            </button>
            {active === s.index && s.snippet && (
              <div className="ui-source-snippet">{s.snippet}</div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default memo(Message);
