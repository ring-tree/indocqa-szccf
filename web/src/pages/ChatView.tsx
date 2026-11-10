import { memo, useEffect, useRef, useState } from "react";
import Message from "../components/Message";
import ModelPicker from "../components/ModelPicker";
import {
  createSession,
  fetchFolders,
  fetchModels,
  fetchSession,
  sendChat,
  type ChatMode,
} from "../lib/api";
import type { ChatMessage, DonePayload, KBFolder, ModelProfile } from "../lib/types";

interface Props {
  activeId: string | null;
  onActiveId: (id: string | null) => void;
  onRefreshSessions: () => void;
  className?: string;
  examples?: string[];
  active?: boolean;
}

const SCOPE_KEY = "indocqa-scope";

const ERROR_HINT: Record<number, string> = {
  40001: "请求参数不合法，请检查问题或检索范围",
  40002: "文件超过 20MB 上限",
  40003: "文件格式不支持",
  40401: "知识库为空，请先在文档管理页入库",
  40402: "文档或目录不存在，请刷新后重试",
  40403: "会话不存在或已过期，请新建会话",
  40902: "向量库与当前嵌入后端不一致，需要重建索引",
  40903: "知识库正在被另一个进程写入，请稍后再试",
  42901: "模型限流或不可用，已尝试主备切换，请稍后重试",
  50001: "检索服务异常（嵌入或向量库），请检查服务状态",
};

function describeError(code: number | undefined, message: string): string {
  const hint = code ? ERROR_HINT[code] : undefined;
  return hint ? `${hint}（${message}）` : message;
}

function ChatView({
  activeId,
  onActiveId,
  onRefreshSessions,
  className = "",
  examples = [],
  active = true,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<ChatMode>("kb");
  const [busy, setBusy] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [profile, setProfile] = useState("");
  const [folders, setFolders] = useState<KBFolder[]>([]);
  const [folder, setFolder] = useState(() => localStorage.getItem(SCOPE_KEY) ?? "");
  const [notice, setNotice] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const currentRef = useRef<string | null>(null);
  const runIdRef = useRef(0);
  const bufferRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);
  const hasModels = models.length > 0;

  useEffect(() => {
    fetchModels()
      .then((list) => {
        setModels(list);
        setProfile(list[0]?.id ?? "");
      })
      .catch(() => setModels([]))
      .finally(() => setModelsLoaded(true));
    fetchFolders()
      .then((list) => {
        setFolders(list);
        setFolder((current) => (list.some((f) => f.path === current) ? current : ""));
      })
      .catch(() => setFolders([]));
  }, []);

  useEffect(() => {
    localStorage.setItem(SCOPE_KEY, folder);
  }, [folder]);

  // 文档页增删目录后回到问答页时刷新范围列表
  useEffect(() => {
    if (!active) return;
    fetchFolders()
      .then((list) => {
        setFolders(list);
        setFolder((current) => (list.some((f) => f.path === current) ? current : ""));
      })
      .catch(() => setFolders([]));
  }, [active]);

  useEffect(() => {
    if (activeId === currentRef.current) return;
    currentRef.current = activeId;
    // 切换会话时中止在途生成，并用 runId 让迟到的 token 失效，避免写入错误会话
    runIdRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    bufferRef.current = "";
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setBusy(false);
    if (!activeId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    fetchSession(activeId)
      .then((session) => {
        if (!cancelled) setMessages(session.messages ?? []);
      })
      .catch(() => {
        if (!cancelled) setMessages([]);
      });
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  useEffect(() => {
    if (!atBottom) return;
    const frame = requestAnimationFrame(() =>
      bottomRef.current?.scrollIntoView({ block: "end" }),
    );
    return () => cancelAnimationFrame(frame);
  }, [messages, atBottom]);

  function patchLast(fn: (m: ChatMessage) => ChatMessage) {
    setMessages((ms) => ms.map((m, i) => (i === ms.length - 1 ? fn(m) : m)));
  }

  function changeMode(next: ChatMode) {
    if (next === mode || busy) return;
    if (next === "direct" && !hasModels) return;
    setMode(next);
    if (messages.length > 0) {
      onActiveId(null);
      setMessages([]);
      setNotice("已切换模式，下一条消息将开启新会话");
    }
  }

  function selectProfile(value: string) {
    setProfile(value);
  }

  useEffect(() => {
    if (!hasModels && mode === "direct") setMode("kb");
  }, [hasModels, mode]);

  function flushBuffer() {
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const text = bufferRef.current;
    if (!text) return;
    bufferRef.current = "";
    patchLast((m) => ({ ...m, content: m.content + text }));
  }

  async function ask(retryText?: string) {
    const text = (retryText ?? input).trim();
    if (!text || busy) return;
    if (mode === "direct" && !hasModels) return;
    if (!retryText) setInput("");
    setNotice("");

    let sid = activeId;
    if (!sid) {
      try {
        const session = await createSession(text.slice(0, 40));
        sid = session.id;
        currentRef.current = sid;
        onActiveId(sid);
        onRefreshSessions();
      } catch {
        setInput(text);
        return;
      }
    }

    const runId = ++runIdRef.current;
    bufferRef.current = "";
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const alive = () => runId === runIdRef.current;
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setAtBottom(true);
    setMessages((ms) => [
      ...ms,
      { role: "user", content: text },
      { role: "assistant", content: "", streaming: true, trace: [] },
    ]);

    try {
      await sendChat(
        text,
        mode,
        sid,
        {
          onSources: (items) => {
            if (!alive()) return;
            patchLast((m) => ({ ...m, sources: items }));
          },
          onToken: (delta) => {
            if (!alive()) return;
            // 40ms 批量刷入，避免每个 token 都触发整段 Markdown 重解析
            bufferRef.current += delta;
            if (flushTimerRef.current === null) {
              flushTimerRef.current = window.setTimeout(() => {
                flushTimerRef.current = null;
                flushBuffer();
              }, 40);
            }
          },
          onTrace: (step) => {
            if (!alive()) return;
            patchLast((m) => ({ ...m, trace: [...(m.trace ?? []), step] }));
          },
          onDone: (done: DonePayload) => {
            if (!alive()) return;
            flushBuffer();
            patchLast((m) => ({
              ...m,
              id: done.messageId ?? undefined,
              confidence: done.confidence,
              confidenceReason: done.confidence_reason,
              boundary: done.boundary,
              model: done.model,
            }));
            if (done.sessionId && done.sessionId !== sid) {
              currentRef.current = done.sessionId;
              onActiveId(done.sessionId);
            }
          },
          onError: (message, code) => {
            if (!alive()) return;
            flushBuffer();
            patchLast((m) => ({
              ...m,
              error: true,
              content: `${m.content}${m.content ? "\n" : ""}[请求失败] ${describeError(
                code,
                message,
              )}`,
            }));
          },
        },
        controller.signal,
        profile,
        folder || null,
      );
    } catch (e) {
      if (!alive()) return;
      if (controller.signal.aborted) {
        flushBuffer();
        patchLast((m) => ({
          ...m,
          stopped: true,
          content: m.content || "已停止生成。",
        }));
      } else {
        flushBuffer();
        patchLast((m) => ({
          ...m,
          error: true,
          content: `${m.content}${m.content ? "\n" : ""}[请求失败] ${
            e instanceof Error ? e.message : "网络异常"
          }`,
        }));
      }
    } finally {
      if (alive()) {
        flushBuffer();
        patchLast((m) => ({ ...m, streaming: false }));
        setBusy(false);
        abortRef.current = null;
        onRefreshSessions();
      }
    }
  }

  function stop() {
    flushBuffer();
    runIdRef.current += 1; // 让迟到的 token 失效
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    patchLast((m) => ({ ...m, streaming: false, stopped: true }));
    onRefreshSessions();
  }

  function retryLast() {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser) void ask(lastUser.content);
  }

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  }

  return (
    <div className={`ui-chat-view flex min-h-0 flex-1 flex-col ${className}`}>
      <div className="ui-modebar flex flex-wrap items-center justify-between gap-2 px-4 py-3 sm:px-6">
        <div
          role="group"
          aria-label="问答模式"
          className="ui-mode-group"
        >
          {(
            [
              ["kb", "知识库问答"],
              ["direct", "直接对话"],
            ] as [ChatMode, string][]
          ).map(([value, label]) => {
            const disabled = busy || (value === "direct" && !hasModels);
            return (
              <button
                key={value}
                type="button"
                onClick={() => changeMode(value)}
                disabled={disabled}
                aria-pressed={mode === value}
                title={
                  value === "direct" && !hasModels
                    ? "未配置输出模型，直接对话不可用"
                    : undefined
                }
                className={`ui-mode-btn ${mode === value ? "is-active" : ""}`}
              >
                {label}
              </button>
            );
          })}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {mode === "kb" && (
            <label className="ui-scope">
              <span className="ui-scope-label">检索范围</span>
              <select
                value={folder}
                onChange={(e) => setFolder(e.target.value)}
                disabled={busy}
                aria-label="选择知识库检索范围"
                className="ui-scope-select"
              >
                <option value="">全部知识库</option>
                {folders.map((item) => (
                  <option key={item.path} value={item.path}>
                    {item.path}（{item.documentCount}）
                  </option>
                ))}
              </select>
            </label>
          )}
          <span className="hidden text-xs font-medium text-[var(--muted)] sm:block">
            {mode === "kb" ? "回答仅依据已入库文档，逐句附引用" : "不经过知识库检索"}
          </span>
        </div>
      </div>

      <div className="sr-only" role="status" aria-live="polite">
        {busy ? "正在生成回答" : ""}
      </div>

      {notice && (
        <div
          role="status"
          aria-live="polite"
          className="ui-notice flex items-center justify-between px-4 py-2 sm:px-6"
        >
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice("")}
            className="ui-btn ui-btn-sm ui-btn-ghost"
            aria-label="关闭提示"
          >
            ×
          </button>
        </div>
      )}

      {modelsLoaded && !hasModels && (
        <div role="status" className="ui-degraded px-4 py-2 sm:px-6">
          未配置生成模型，知识库问答为原文摘录/拒答模式；直接对话不可用。
        </div>
      )}

      <section
        ref={scrollRef}
        onScroll={onScroll}
        aria-label="对话消息"
        aria-busy={busy}
        className="ui-message-area chat-scroll min-h-0 flex-1 space-y-6 overflow-y-auto px-4 py-6 sm:px-6"
      >
        {messages.length === 0 && (
          <div className="ui-empty-hero mx-auto mt-6 max-w-2xl px-6 py-8 text-center sm:mt-10 sm:py-10">
            <div className="ui-empty-badge">可追溯知识库问答</div>
            <h2 className="mt-4 text-2xl font-bold tracking-tight text-[var(--ink)] sm:text-3xl">
              今天想查点什么？
            </h2>
            <p className="mx-auto mt-3 max-w-lg text-sm leading-6 text-[var(--muted)]">
              每一条回答都会标注来源文档、章节与页码；知识库没有覆盖的内容会明确说明，避免把推测当成事实。
            </p>
            <div className="mt-7 space-y-2 text-left">
              {examples.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => setInput(ex)}
                  className="ui-example-btn"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <Message
            key={i}
            message={m}
            anchor={`msg-${i}`}
            onRetry={m.error ? retryLast : undefined}
          />
        ))}
        <div ref={bottomRef} />
      </section>

      <footer aria-label="消息输入区" className="ui-composer px-4 py-3 sm:px-6">
        <div className="ui-composer-shell p-2">
          <label htmlFor="chat-input" className="sr-only">
            输入问题
          </label>
          <textarea
            id="chat-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void ask();
              }
            }}
            rows={2}
            maxLength={2000}
            disabled={mode === "direct" && !hasModels}
            aria-describedby="chat-hint"
            placeholder={
              mode === "direct" && !hasModels
                ? "未配置输出模型，直接对话不可用"
                : mode === "kb"
                  ? "输入问题，Enter 发送，Shift+Enter 换行"
                  : "直接与模型对话"
            }
            className="ui-textarea max-h-40 min-h-[2.75rem] w-full resize-none bg-transparent px-3 py-2 text-sm outline-none disabled:cursor-not-allowed"
          />
          <span id="chat-hint" className="sr-only">
            按 Enter 发送，Shift 加 Enter 换行，最多 2000 字
          </span>
          <div className="flex items-center justify-between gap-2 px-1">
            <div className="flex min-w-0 items-center gap-2">
              <ModelPicker
                models={models}
                profile={profile}
                onSelect={selectProfile}
                disabled={!hasModels || busy}
              />
              <span aria-hidden="true" className="ui-counter hidden sm:block">
                {busy ? "生成中，可随时停止" : `${input.length}/2000`}
              </span>
            </div>
            {busy ? (
              <button
                type="button"
                onClick={stop}
                aria-label="停止生成"
                className="ui-btn ui-btn-stop"
              >
                停止生成
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void ask()}
                disabled={!input.trim() || (mode === "direct" && !hasModels)}
                aria-label="发送消息"
                className="ui-btn ui-btn-primary"
              >
                发送
              </button>
            )}
          </div>
        </div>
      </footer>
    </div>
  );
}

export default memo(ChatView);
