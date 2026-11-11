import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
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

const SCOPE_KEY = "indocqa-scope-paths";

function readStoredScope(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SCOPE_KEY) ?? "[]");
    return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

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

function ScopeCheckbox({
  checked,
  indeterminate,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate: boolean;
  label: string;
  onChange: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      onChange={onChange}
      aria-label={label}
      aria-checked={indeterminate ? "mixed" : checked}
    />
  );
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
  const [scopePaths, setScopePaths] = useState<string[]>(readStoredScope);
  const [scopeOpen, setScopeOpen] = useState(false);
  const scopeRef = useRef<HTMLDivElement>(null);
  const scopeTouchedRef = useRef(scopePaths.length > 0);
  const [notice, setNotice] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const textareaWidthRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const currentRef = useRef<string | null>(null);
  const runIdRef = useRef(0);
  const bufferRef = useRef("");
  const flushTimerRef = useRef<number | null>(null);
  const hasModels = models.length > 0;

  const childrenMap = useMemo(() => {
    const map = new Map<string, KBFolder[]>();
    for (const item of folders) {
      const list = map.get(item.parent) ?? [];
      list.push(item);
      map.set(item.parent, list);
    }
    for (const list of map.values()) list.sort((a, b) => a.name.localeCompare(b.name));
    return map;
  }, [folders]);

  function childrenOf(path: string): KBFolder[] {
    return childrenMap.get(path) ?? [];
  }

  function descendantsOf(path: string): KBFolder[] {
    const result: KBFolder[] = [];
    const walk = (parent: string) => {
      for (const child of childrenOf(parent)) {
        result.push(child);
        walk(child.path);
      }
    };
    walk(path);
    return result;
  }

  function isScopeChecked(path: string): boolean {
    if (scopePaths.includes(path)) return true;
    const children = childrenOf(path);
    return children.length > 0 && children.every((child) => isScopeChecked(child.path));
  }

  function isScopeIndeterminate(path: string): boolean {
    if (isScopeChecked(path)) return false;
    return descendantsOf(path).some((child) => scopePaths.includes(child.path));
  }

  function toggleScope(path: string) {
    scopeTouchedRef.current = true;
    const checked = isScopeChecked(path);
    const descendants = descendantsOf(path).map((item) => item.path);
    const parts = path.split("/");
    const ancestors = parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join("/"));
    setScopePaths((current) => {
      const next = new Set(current);
      if (checked) {
        for (const item of [path, ...descendants, ...ancestors]) next.delete(item);
      } else {
        for (const item of [path, ...descendants]) next.add(item);
      }
      return [...next];
    });
  }

  function toggleAllScope() {
    scopeTouchedRef.current = true;
    setScopePaths(allSelected ? [] : folders.map((item) => item.path));
  }

  const restoreAllIfEmpty = useCallback(() => {
    // 未选择任何目录等价于全库检索；面板开合时恢复全选，保持与后端一致
    if (scopePaths.length === 0 && folders.length > 0) {
      setScopePaths(folders.map((item) => item.path));
      scopeTouchedRef.current = false;
    }
  }, [scopePaths, folders]);

  function toggleScopePanel() {
    restoreAllIfEmpty();
    setScopeOpen(!scopeOpen);
  }

  function renderScopeNodes(parent: string, depth: number): ReactNode[] {
    return childrenOf(parent).flatMap((item) => [
      <label
        key={item.path}
        className="ui-scope-node"
        style={{ paddingLeft: 8 + depth * 14 }}
      >
        <ScopeCheckbox
          checked={isScopeChecked(item.path)}
          indeterminate={isScopeIndeterminate(item.path)}
          onChange={() => toggleScope(item.path)}
          label={`选择目录 ${item.path}`}
        />
        <span className="ui-scope-node-name">{item.name}</span>
        <span className="ui-scope-node-count">{item.documentCount}</span>
      </label>,
      ...renderScopeNodes(item.path, depth + 1),
    ]);
  }

  const scopeRoots = scopePaths.filter(
    (path) => !scopePaths.some((other) => other !== path && path.startsWith(other + "/")),
  );
  const allSelected =
    folders.length > 0 &&
    folders.filter((item) => !item.parent).every((item) => isScopeChecked(item.path));
  const scopeLabel =
    scopePaths.length === 0 || allSelected
      ? "全部知识库"
      : scopeRoots.length === 1
        ? scopeRoots[0]
        : `已选 ${scopeRoots.length} 个目录`;

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
        setScopePaths((current) => {
          // 未发生用户操作时保持默认全选（包含新增目录）
          if (!scopeTouchedRef.current) return list.map((f) => f.path);
          return current.filter((p) => list.some((f) => f.path === p));
        });
      })
      .catch(() => setFolders([]));
  }, []);

  useEffect(() => {
    localStorage.setItem(SCOPE_KEY, JSON.stringify(scopePaths));
  }, [scopePaths]);

  // 文档页增删目录后回到问答页时刷新范围列表
  useEffect(() => {
    if (!active) return;
    fetchFolders()
      .then((list) => {
        setFolders(list);
        setScopePaths((current) => {
          if (!scopeTouchedRef.current) return list.map((f) => f.path);
          return current.filter((p) => list.some((f) => f.path === p));
        });
      })
      .catch(() => setFolders([]));
  }, [active]);

  useEffect(() => {
    if (!scopeOpen) return;
    const close = () => {
      restoreAllIfEmpty();
      setScopeOpen(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!scopeRef.current?.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [scopeOpen, restoreAllIfEmpty]);

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
              plainText: done.plainText,
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
        null,
        scopePaths.length > 0 ? scopePaths : null,
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

  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const styles = getComputedStyle(el);
    const min = parseFloat(styles.minHeight) || 0;
    const max = parseFloat(styles.maxHeight) || Number.POSITIVE_INFINITY;
    el.style.height = "auto";
    const next = Math.max(min, Math.min(el.scrollHeight, max));
    el.style.height = `${next}px`;
  }, []);

  useLayoutEffect(() => {
    resizeTextarea();
  }, [input, resizeTextarea]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (Math.abs(width - textareaWidthRef.current) < 0.5) return;
      textareaWidthRef.current = width;
      resizeTextarea();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [resizeTextarea]);

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
            <div className="ui-scope" ref={scopeRef}>
              <button
                type="button"
                className="ui-scope-trigger"
                aria-haspopup="dialog"
                aria-expanded={scopeOpen}
                aria-controls="scope-popover"
                onClick={toggleScopePanel}
              >
                <span className="ui-scope-label">检索范围</span>
                <span className="ui-scope-value">{scopeLabel}</span>
                <span className="ui-scope-chevron" aria-hidden="true">▾</span>
              </button>
              {scopeOpen && (
                <div
                  id="scope-popover"
                  className="ui-scope-popover"
                  role="dialog"
                  aria-label="选择检索目录"
                >
                  <div className="ui-scope-popover-head">
                    <span>选择检索范围</span>
                    <button
                      type="button"
                      className="ui-btn ui-btn-sm ui-btn-ghost"
                      onClick={toggleAllScope}
                      aria-pressed={allSelected}
                      title={allSelected ? "取消全选" : "全选所有目录"}
                      disabled={folders.length === 0}
                    >
                      全部知识库
                    </button>
                  </div>
                  <div className="ui-scope-tree">
                    {folders.length === 0 && (
                      <p className="ui-scope-empty">还没有目录，上传文档后可选择范围</p>
                    )}
                    {renderScopeNodes("", 0)}
                  </div>
                </div>
              )}
            </div>
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

        {messages.length > 0 && (
          <div className="ui-thread">
            {messages.map((m, i) => (
              <Message
                key={i}
                message={m}
                anchor={`msg-${i}`}
                onRetry={m.error ? retryLast : undefined}
              />
            ))}
          </div>
        )}
        <div ref={bottomRef} />
      </section>

      <footer aria-label="消息输入区" className="ui-composer px-4 py-3 sm:px-6">
        <div className="ui-composer-shell p-2">
          <label htmlFor="chat-input" className="sr-only">
            输入问题
          </label>
          <textarea
            ref={textareaRef}
            id="chat-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void ask();
              }
            }}
            rows={1}
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
            className="ui-textarea w-full bg-transparent px-3 py-2 text-sm outline-none disabled:cursor-not-allowed"
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
