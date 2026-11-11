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
import AttachmentTray from "../components/AttachmentTray";
import AttachmentUploader from "../components/AttachmentUploader";
import Message from "../components/Message";
import ModelPicker from "../components/ModelPicker";
import { MAX_FILES, validateFiles } from "../lib/attachments";
import {
  abortUpload,
  completeUpload,
  createSession,
  deleteAttachment,
  fetchFolders,
  fetchModels,
  fetchSession,
  fetchUploadStatus,
  initUpload,
  sendChat,
  uploadChunk,
  type ChatMode,
} from "../lib/api";
import type {
  AttachmentInfo,
  ChatMessage,
  DonePayload,
  KBFolder,
  ModelProfile,
  PendingAttachment,
} from "../lib/types";

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
  40002: "文件超过 100MB 上限",
  40003: "文件格式不支持",
  40004: "单条消息附件数量超过 10 个",
  40005: "上传任务不存在或已过期，请重新上传",
  40006: "上传分片不完整或大小不合法，请重试",
  40301: "无权访问该附件或会话，请新建会话后重试",
  40401: "知识库为空，请先在文档管理页入库",
  40402: "文档或目录不存在，请刷新后重试",
  40403: "会话不存在或已过期，请新建会话",
  40404: "附件不存在或已删除",
  40901: "上传状态冲突，请稍后重试",
  40902: "向量库与当前嵌入后端不一致，需要重建索引",
  40903: "知识库正在被另一个进程写入，请稍后再试",
  42901: "模型限流或不可用，已尝试主备切换，请稍后重试",
  50001: "检索服务异常（嵌入或向量库），请检查服务状态",
  50003: "附件存储异常，请重试或更换文件",
};

const STREAM_IDLE_TIMEOUT_MS = 45_000;

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
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const uploadSessionRef = useRef<string | null>(activeId);
  const uploadSessionPromiseRef = useRef<Promise<string> | null>(null);
  const uploadQueueRef = useRef<PendingAttachment[]>([]);
  const activeUploadsRef = useRef(0);
  const uploadAbortRef = useRef<Map<string, AbortController>>(new Map());
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const textareaWidthRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const currentRef = useRef<string | null>(null);
  const runIdRef = useRef(0);
  const typeQueueRef = useRef("");
  const typeTimerRef = useRef<number | null>(null);
  const typeSettleRef = useRef<(() => void) | null>(null);
  const hasModels = models.length > 0;
  const readyAttachmentCount = pending.filter(
    (item) => item.status === "ready" && item.attachment,
  ).length;
  const hasUnreadyAttachment = pending.some((item) => item.status !== "ready");

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
    // 切换会话时丢弃未展示的字符，避免写入错误会话
    if (typeTimerRef.current !== null) {
      window.clearInterval(typeTimerRef.current);
      typeTimerRef.current = null;
    }
    typeQueueRef.current = "";
    typeSettleRef.current?.();
    typeSettleRef.current = null;
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
    // 用户切换会话/新建会话时中止在途上传；本组件刚创建上传会话时 sessionRef 已同步，不会误清空
    if (uploadSessionRef.current === activeId) return;
    uploadQueueRef.current = [];
    uploadAbortRef.current.forEach((controller) => controller.abort());
    uploadAbortRef.current.clear();
    setPending([]);
    uploadSessionRef.current = activeId;
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

  function patchPending(localId: string, patch: Partial<PendingAttachment>) {
    setPending((items) =>
      items.map((item) => (item.localId === localId ? { ...item, ...patch } : item)),
    );
  }

  function newLocalId(): string {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
    return `att-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function isAbortError(error: unknown): boolean {
    return error instanceof Error && error.name === "AbortError";
  }

  function describeUploadError(error: unknown): string {
    const item = error as Error & { code?: number };
    const hint = item.code ? ERROR_HINT[item.code] : undefined;
    return hint ? `${hint}（${item.message}）` : item.message || "上传失败";
  }

  async function ensureUploadSession(filename: string): Promise<string> {
    if (activeId) {
      uploadSessionRef.current = activeId;
      return activeId;
    }
    if (uploadSessionPromiseRef.current) return uploadSessionPromiseRef.current;
    const promise = createSession(filename.slice(0, 40) || "附件会话")
      .then((session) => {
        uploadSessionRef.current = session.id;
        currentRef.current = session.id;
        onActiveId(session.id);
        onRefreshSessions();
        return session.id;
      })
      .finally(() => {
        uploadSessionPromiseRef.current = null;
      });
    uploadSessionPromiseRef.current = promise;
    return promise;
  }

  async function uploadOne(item: PendingAttachment): Promise<void> {
    const controller = new AbortController();
    uploadAbortRef.current.set(item.localId, controller);
    try {
      const sid = await ensureUploadSession(item.file.name);
      if (controller.signal.aborted) return;

      let uploadId = item.uploadId;
      let chunkSize = item.chunkSize;
      let totalChunks = 0;
      let received = new Set<number>();

      if (uploadId) {
        try {
          const status = await fetchUploadStatus(uploadId, sid);
          if (status.status === "completing") {
            throw new Error("上次上传仍在处理中，请稍后重试");
          }
          chunkSize = status.chunkSize;
          totalChunks = status.totalChunks;
          received = new Set(status.received);
        } catch (error) {
          if (isAbortError(error) || controller.signal.aborted) throw error;
          const code = (error as Error & { code?: number }).code;
          // 只有上传任务已失效才重新初始化；网络错误保留 uploadId 以便续传
          if (code === 40005) {
            uploadId = undefined;
          } else {
            throw error;
          }
        }
      }

      if (!uploadId) {
        const init = await initUpload({
          sessionId: sid,
          filename: item.file.name,
          size: item.file.size,
          mime: item.file.type || undefined,
        });
        uploadId = init.uploadId;
        chunkSize = init.chunkSize;
        totalChunks = init.totalChunks;
        received = new Set(init.received);
        patchPending(item.localId, { uploadId, chunkSize, progress: 0 });
      }
      if (!uploadId || !chunkSize || !totalChunks) throw new Error("上传初始化失败");
      const activeUploadId = uploadId;
      const activeChunkSize = chunkSize;
      const activeTotalChunks = totalChunks;

      const bytesOf = (index: number) =>
        Math.max(
          0,
          Math.min(activeChunkSize, item.file.size - index * activeChunkSize),
        );
      let uploadedBefore = [...received].reduce((sum, index) => sum + bytesOf(index), 0);

      for (let index = 0; index < activeTotalChunks; index += 1) {
        if (received.has(index)) continue;
        const start = index * activeChunkSize;
        const end = Math.min(item.file.size, start + activeChunkSize);
        const chunk = item.file.slice(start, end);
        if (chunk.size === 0) continue;

        let attempt = 0;
        for (;;) {
          try {
            await uploadChunk(
              activeUploadId,
              index,
              chunk,
              sid,
              (ratio) => {
                const overall = Math.round(
                  (100 * (uploadedBefore + ratio * chunk.size)) / item.file.size,
                );
                patchPending(item.localId, { progress: Math.min(99, overall) });
              },
              controller.signal,
            );
            break;
          } catch (error) {
            if (controller.signal.aborted || isAbortError(error)) throw error;
            attempt += 1;
            if (attempt >= 3) throw error;
            await sleep(300 * attempt);
          }
        }
        uploadedBefore += chunk.size;
        patchPending(item.localId, {
          progress: Math.min(99, Math.round((100 * uploadedBefore) / item.file.size)),
        });
      }

      const attachment = await completeUpload(activeUploadId, sid);
      patchPending(item.localId, {
        status: "ready",
        progress: 100,
        attachment,
        uploadId: undefined,
      });
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      patchPending(item.localId, {
        status: "error",
        error: describeUploadError(error),
      });
    } finally {
      uploadAbortRef.current.delete(item.localId);
    }
  }

  function drainUploadQueue() {
    while (activeUploadsRef.current < 2) {
      const next = uploadQueueRef.current.shift();
      if (!next) break;
      activeUploadsRef.current += 1;
      void uploadOne(next).finally(() => {
        activeUploadsRef.current = Math.max(0, activeUploadsRef.current - 1);
        drainUploadQueue();
      });
    }
  }

  function addFiles(files: File[]) {
    if (busy) {
      setNotice("生成回答时暂不能添加附件");
      return;
    }
    const remaining = MAX_FILES - pending.length;
    const { accepted, errors } = validateFiles(files, remaining);
    setNotice(errors.join("；"));
    if (accepted.length === 0) return;
    const items: PendingAttachment[] = accepted.map((file) => ({
      localId: newLocalId(),
      file,
      status: "uploading",
      progress: 0,
    }));
    setPending((current) => [...current, ...items]);
    uploadQueueRef.current.push(...items);
    drainUploadQueue();
  }

  function retryUpload(localId: string) {
    const item = pending.find((candidate) => candidate.localId === localId);
    if (!item) return;
    setNotice("");
    const next: PendingAttachment = {
      ...item,
      status: "uploading",
      progress: 0,
      error: undefined,
    };
    patchPending(localId, { status: "uploading", progress: 0, error: undefined });
    uploadQueueRef.current.push(next);
    drainUploadQueue();
  }

  function removePending(localId: string) {
    const item = pending.find((candidate) => candidate.localId === localId);
    uploadQueueRef.current = uploadQueueRef.current.filter((entry) => entry.localId !== localId);
    uploadAbortRef.current.get(localId)?.abort();
    uploadAbortRef.current.delete(localId);
    setPending((items) => items.filter((entry) => entry.localId !== localId));
    const sid = uploadSessionRef.current;
    if (!sid || !item) return;
    if (item.attachment) {
      void deleteAttachment(item.attachment.id, sid).catch(() => undefined);
    } else if (item.uploadId) {
      void abortUpload(item.uploadId, sid).catch(() => undefined);
    }
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

  /** 停止打字机；flush=true 时把尚未展示的字符一次性补进当前消息。 */
  function stopTypewriter(flush: boolean) {
    if (typeTimerRef.current !== null) {
      window.clearInterval(typeTimerRef.current);
      typeTimerRef.current = null;
    }
    const text = typeQueueRef.current;
    typeQueueRef.current = "";
    if (flush && text) patchLast((m) => ({ ...m, content: m.content + text }));
    const settle = typeSettleRef.current;
    typeSettleRef.current = null;
    settle?.();
  }

  /** 逐字展示 SSE 下发的增量；积压较多时自适应加速，避免落后于流式输出。 */
  function startTypewriter(alive: () => boolean) {
    if (typeTimerRef.current !== null) return;
    typeTimerRef.current = window.setInterval(() => {
      if (!alive()) {
        stopTypewriter(false);
        return;
      }
      const queue = typeQueueRef.current;
      if (!queue) {
        const timer = typeTimerRef.current;
        if (timer !== null) window.clearInterval(timer);
        typeTimerRef.current = null;
        const settle = typeSettleRef.current;
        typeSettleRef.current = null;
        settle?.();
        return;
      }
      const step =
        queue.length > 60
          ? Math.min(16, Math.ceil(queue.length / 24))
          : queue.length > 16
            ? 2
            : 1;
      const chunk = queue.slice(0, step);
      typeQueueRef.current = queue.slice(step);
      patchLast((m) => ({ ...m, content: m.content + chunk }));
    }, 24);
  }

  /** 等待队列中剩余字符展示完毕，保证 done 后动画完整收尾。 */
  function waitTypewriter(): Promise<void> {
    if (!typeQueueRef.current && typeTimerRef.current === null) return Promise.resolve();
    return new Promise((resolve) => {
      typeSettleRef.current = resolve;
    });
  }

  async function ask(retryText?: string, retryAttachments?: AttachmentInfo[]) {
    const text = (retryText ?? input).trim();
    const readyAttachments =
      retryAttachments ??
      pending
        .filter((item) => item.status === "ready" && item.attachment)
        .map((item) => item.attachment as AttachmentInfo);
    const pendingNotReady = !retryAttachments && pending.some((item) => item.status !== "ready");
    if ((!text && readyAttachments.length === 0) || busy || pendingNotReady) {
      if (pendingNotReady) setNotice("请等待附件上传完成后再发送");
      return;
    }
    if (mode === "direct" && !hasModels) return;
    if (!retryText) setInput("");
    setNotice("");

    let sid = activeId;
    if (!sid) {
      try {
        const title =
          text.slice(0, 40) ||
          readyAttachments[0]?.filename?.slice(0, 40) ||
          "附件会话";
        const session = await createSession(title);
        sid = session.id;
        currentRef.current = sid;
        onActiveId(sid);
        onRefreshSessions();
      } catch {
        setInput(text);
        return;
      }
    }
    if (!retryText) {
      setPending([]);
      uploadQueueRef.current = [];
    }

    const runId = ++runIdRef.current;
    stopTypewriter(false);
    const alive = () => runId === runIdRef.current;
    const controller = new AbortController();
    abortRef.current = controller;
    let lastActivity = Date.now();
    let watchdogTriggered = false;
    let doneReceived = false;
    let failed = false;
    const touch = () => {
      lastActivity = Date.now();
    };
    const watchdog = window.setInterval(() => {
      if (Date.now() - lastActivity > STREAM_IDLE_TIMEOUT_MS) {
        watchdogTriggered = true;
        controller.abort();
      }
    }, 5000);
    setBusy(true);
    setAtBottom(true);
    setMessages((ms) => [
      ...ms,
      { role: "user", content: text, attachments: readyAttachments },
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
            touch();
            patchLast((m) => ({ ...m, sources: items }));
          },
          onToken: (delta) => {
            if (!alive()) return;
            touch();
            // 进入打字机队列逐字展示；Markdown 重解析频率由展示节奏控制
            typeQueueRef.current += delta;
            startTypewriter(alive);
          },
          onTrace: (step) => {
            if (!alive()) return;
            touch();
            patchLast((m) => ({ ...m, trace: [...(m.trace ?? []), step] }));
          },
          onDone: (done: DonePayload) => {
            if (!alive()) return;
            touch();
            doneReceived = true;
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
            failed = true;
            stopTypewriter(true);
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
        readyAttachments.map((item) => item.id),
      );
    } catch (e) {
      if (!alive()) return;
      failed = true;
      if (controller.signal.aborted) {
        stopTypewriter(true);
        patchLast((m) =>
          watchdogTriggered
            ? {
                ...m,
                error: true,
                content: `${m.content}${m.content ? "\n" : ""}[响应超时] 超过 45 秒未收到新数据，请重试。`,
              }
            : {
                ...m,
                stopped: true,
                content: m.content || "已停止生成。",
              },
        );
      } else {
        stopTypewriter(true);
        patchLast((m) => ({
          ...m,
          error: true,
          content: `${m.content}${m.content ? "\n" : ""}[请求失败] ${
            e instanceof Error ? e.message : "网络异常"
          }`,
        }));
      }
    } finally {
      window.clearInterval(watchdog);
      // 等待打字机把 done 前收到的内容逐字展示完，再收尾
      await waitTypewriter();
      if (alive()) {
        if (!doneReceived && !failed) {
          patchLast((m) => ({
            ...m,
            error: true,
            content: `${m.content}${m.content ? "\n" : ""}[连接中断] 服务端未返回完成信号，请重试。`,
          }));
        }
        patchLast((m) => ({ ...m, streaming: false }));
        setBusy(false);
        abortRef.current = null;
        onRefreshSessions();
      }
    }
  }

  function stop() {
    stopTypewriter(true);
    runIdRef.current += 1; // 让迟到的 token 失效
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    patchLast((m) => ({ ...m, streaming: false, stopped: true }));
    onRefreshSessions();
  }

  function retryLast() {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser) void ask(lastUser.content, lastUser.attachments);
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

      <footer
        aria-label="消息输入区"
        className="ui-composer relative px-4 py-3 sm:px-6"
        onDragOver={(event) => {
          if (busy) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
          setDragActive(true);
        }}
        onDragLeave={(event) => {
          const target = event.relatedTarget as Node | null;
          if (!target || !event.currentTarget.contains(target)) setDragActive(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragActive(false);
          if (!busy) addFiles(Array.from(event.dataTransfer.files));
        }}
      >
        {dragActive && (
          <div className="ui-drop-overlay">
            <span>松开以上传附件</span>
          </div>
        )}
        <div className="ui-composer-shell p-2">
          <AttachmentTray items={pending} onRetry={retryUpload} onRemove={removePending} />
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
                  ? "输入问题，可拖拽/添加附件，Enter 发送"
                  : "直接与模型对话，可拖拽/添加附件"
            }
            className="ui-textarea w-full bg-transparent px-3 py-2 text-sm outline-none disabled:cursor-not-allowed"
          />
          <span id="chat-hint" className="sr-only">
            按 Enter 发送，Shift 加 Enter 换行，最多 2000 字
          </span>
          <div className="flex items-center justify-between gap-2 px-1">
            <div className="flex min-w-0 items-center gap-2">
              <AttachmentUploader onFiles={addFiles} disabled={busy} />
              <ModelPicker
                models={models}
                profile={profile}
                onSelect={selectProfile}
                disabled={!hasModels || busy}
              />
              <span aria-hidden="true" className="ui-counter hidden sm:block">
                {busy
                  ? "生成中，可随时停止"
                  : pending.length > 0
                    ? `${readyAttachmentCount}/${pending.length} 附件就绪`
                    : `${input.length}/2000`}
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
                disabled={
                  (!input.trim() && readyAttachmentCount === 0) ||
                  (mode === "direct" && !hasModels) ||
                  hasUnreadyAttachment
                }
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
