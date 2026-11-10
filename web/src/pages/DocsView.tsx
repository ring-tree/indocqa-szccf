import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DragEvent } from "react";
import ModelPicker from "../components/ModelPicker";
import {
  batchMoveDocuments,
  classifyDocument,
  createFolder,
  deleteDocument,
  deleteFolder,
  fetchDocuments,
  fetchFolders,
  fetchHealth,
  fetchIngestJob,
  fetchModels,
  previewDocument,
  renameFolder,
  sendClassifyFeedback,
  startIngestJob,
  undoMove,
  type PreviewReport,
} from "../lib/api";
import type {
  ClassificationResult,
  EmbeddingStatus,
  IngestJob,
  KBDocument,
  KBFolder,
  ModelProfile,
} from "../lib/types";

interface Props {
  className?: string;
  onChanged?: () => void;
}

interface PendingItem {
  file: File;
  report: PreviewReport;
  target: string;
  showAi: boolean;
}

interface ConfirmMove {
  ids: string[];
  source: string;
  target: string;
  suggested?: string;
}

interface BatchAiState {
  open: boolean;
  phase: "confirm" | "running" | "done";
  total: number;
  current: number;
  percent: number;
  message: string;
  result?: { moved: number; skipped: number; low: number; failed: number };
}

const DEFAULT_FOLDER = "未分类";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function vectorLabel(status: EmbeddingStatus | null): string {
  if (!status) return "检测中…";
  if (status.backend.startsWith("ollama-")) return `Ollama · ${status.model}`;
  if (status.backend.startsWith("fe-")) return `本地 · ${status.model}`;
  if (status.backend === "minilm") return "内置 MiniLM";
  return status.backend || status.mode;
}

function confidenceLabel(level: string): string {
  return level === "high" ? "高" : level === "mid" ? "中" : "低";
}

function ConfidenceBadge({ level, value }: { level: string; value: number }) {
  return (
    <span className="ui-confidence" data-tone={level}>
      {confidenceLabel(level)} {Math.round(value * 100)}%
    </span>
  );
}

function PathPicker({
  title,
  value,
  folders,
  onClose,
  onPick,
  onCreate,
}: {
  title: string;
  value: string;
  folders: KBFolder[];
  onClose: () => void;
  onPick: (path: string) => void;
  onCreate: (path: string) => Promise<void>;
}) {
  const [search, setSearch] = useState("");
  const [newPath, setNewPath] = useState("");
  const filtered = folders.filter((f) =>
    f.path.toLowerCase().includes(search.trim().toLowerCase()),
  );
  return (
    <div className="ui-modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="ui-modal ui-path-picker"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="ui-modal-head">
          <h3>{title}</h3>
          <button type="button" className="ui-btn ui-btn-sm ui-btn-ghost" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <input
          type="search"
          className="ui-search w-full px-3 py-2 text-xs outline-none"
          placeholder="搜索目录"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="搜索目录"
        />
        <ul className="ui-picker-list">
          {filtered.map((f) => {
            const depth = f.path.split("/").length - 1;
            return (
              <li key={f.path}>
                <button
                  type="button"
                  onClick={() => onPick(f.path)}
                  style={{ paddingLeft: 10 + depth * 14 }}
                  className={`ui-folder-item ${value === f.path ? "is-active" : ""}`}
                >
                  <span className="truncate">{f.path}</span>
                  <span className="ui-folder-count">{f.documentCount}</span>
                </button>
              </li>
            );
          })}
          {filtered.length === 0 && (
            <li className="px-2 py-3 text-xs text-[var(--muted)]">没有匹配的目录</li>
          )}
        </ul>
        <div className="ui-picker-create">
          <input
            type="text"
            className="ui-search min-w-0 flex-1 px-3 py-2 text-xs outline-none"
            placeholder="新建目录，例如 设备手册/新机组"
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
            aria-label="新建目录路径"
          />
          <button
            type="button"
            className="ui-btn ui-btn-sm ui-btn-ghost"
            disabled={!newPath.trim()}
            onClick={() => void onCreate(newPath.trim())}
          >
            新建并选择
          </button>
        </div>
      </div>
    </div>
  );
}

function ConfirmDialog({
  move,
  docs,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  move: ConfirmMove;
  docs: KBDocument[];
  busy: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const names = move.ids.map((id) => docs.find((d) => d.docId === id)?.filename ?? id);
  return (
    <div className="ui-modal-backdrop" role="presentation" onClick={busy ? undefined : onCancel}>
      <div
        className="ui-modal ui-confirm"
        role="alertdialog"
        aria-modal="true"
        aria-label={`移动 ${move.ids.length} 份文档`}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape" && !busy) onCancel();
        }}
      >
        <div className="ui-modal-head">
          <h3>移动 {move.ids.length} 份文档？</h3>
        </div>
        <dl className="ui-confirm-meta">
          <dt>源目录</dt>
          <dd>{move.source || DEFAULT_FOLDER}</dd>
          <dt>目标目录</dt>
          <dd>{move.target}</dd>
        </dl>
        <p className="ui-confirm-files">
          {names.slice(0, 3).join("、")}
          {names.length > 3 ? ` 等 ${names.length} 份` : ""}
        </p>
        <p className="ui-confirm-hint">移动后 8 秒内可撤销。</p>
        {error && <p className="ui-form-error">{error}</p>}
        <div className="ui-modal-actions">
          <button type="button" className="ui-btn ui-btn-ghost" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button type="button" className="ui-btn ui-btn-primary" disabled={busy} onClick={onConfirm}>
            {busy ? "移动中…" : "确认移动"}
          </button>
        </div>
      </div>
    </div>
  );
}

function AiPanel({
  state,
  onClose,
  onRetry,
  onSelect,
  onAdjust,
  onReject,
  onAccept,
}: {
  state: {
    docId: string;
    filename: string;
    loading: boolean;
    stage: number;
    result?: ClassificationResult;
    selected: string;
    adjusted: boolean;
    error?: string;
  };
  onClose: () => void;
  onRetry: () => void;
  onSelect: (path: string) => void;
  onAdjust: () => void;
  onReject: () => void;
  onAccept: () => void;
}) {
  const result = state.result;
  const candidates = result?.candidates ?? [];
  const reliable = candidates.filter((c) => c.confidence >= 0.6);
  const steps = ["提取文档摘要", "匹配分类规则", "比对目录语义", "生成候选方案"];
  return (
    <div className="ui-modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="ui-modal ui-ai-panel"
        role="dialog"
        aria-modal="true"
        aria-label="AI 推荐目录"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="ui-modal-head">
          <h3>AI 推荐目录</h3>
          <button type="button" className="ui-btn ui-btn-sm ui-btn-ghost" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <p className="ui-ai-doc">{state.filename}</p>

        {state.loading && (
          <ol className="ui-ai-steps">
            {steps.map((step, index) => (
              <li key={step} data-state={index < state.stage ? "done" : index === state.stage ? "active" : "idle"}>
                <span className="ui-ai-step-dot" aria-hidden="true" />
                {step}
              </li>
            ))}
            <li className="ui-ai-wait">通常需要 1–3 秒，最长 8 秒</li>
          </ol>
        )}

        {!state.loading && state.error && (
          <div className="ui-empty-state">
            <p>AI 推荐暂不可用：{state.error}</p>
            <div className="ui-modal-actions">
              <button type="button" className="ui-btn ui-btn-ghost" onClick={onAdjust}>
                手动选择目录
              </button>
              <button type="button" className="ui-btn ui-btn-primary" onClick={onRetry}>
                重试
              </button>
            </div>
          </div>
        )}

        {!state.loading && !state.error && reliable.length === 0 && (
          <div className="ui-empty-state">
            <p>
              暂无可靠推荐
              {candidates[0] ? `（最高置信度 ${Math.round(candidates[0].confidence * 100)}%）` : ""}
            </p>
            <p className="ui-empty-hint">建议手动选择目录，或稍后重试。</p>
            <div className="ui-modal-actions">
              <button type="button" className="ui-btn ui-btn-ghost" onClick={onClose}>
                取消
              </button>
              <button type="button" className="ui-btn ui-btn-primary" onClick={onAdjust}>
                手动选择目录
              </button>
            </div>
          </div>
        )}

        {!state.loading && !state.error && reliable.length > 0 && (
          <>
            <div className="ui-ai-summary">
              {result?.fallback ? "AI 响应超时，已使用规则推荐" : `分析完成 · ${result?.latencyMs ?? 0}ms`}
            </div>
            <ul className="ui-ai-candidates">
              {reliable.map((item) => (
                <li key={item.path}>
                  <label className={`ui-ai-candidate ${state.selected === item.path ? "is-active" : ""}`}>
                    <input
                      type="radio"
                      name="ai-candidate"
                      checked={state.selected === item.path}
                      onChange={() => onSelect(item.path)}
                    />
                    <span className="ui-ai-candidate-main">
                      <span className="ui-ai-path">{item.path}</span>
                      <span className="ui-ai-reason">{item.reason}</span>
                      {item.evidence.length > 0 && (
                        <span className="ui-ai-evidence">证据：{item.evidence.join("、")}</span>
                      )}
                    </span>
                    <ConfidenceBadge level={item.level} value={item.confidence} />
                  </label>
                </li>
              ))}
            </ul>
            {state.adjusted && <p className="ui-ai-adjusted">已手动调整目录，确认后生效。</p>}
            <div className="ui-modal-actions ui-ai-actions">
              <button type="button" className="ui-btn ui-btn-ghost" onClick={onAdjust}>
                调整目录
              </button>
              <span className="flex-1" />
              <button type="button" className="ui-btn ui-btn-ghost" onClick={onReject}>
                驳回
              </button>
              <button
                type="button"
                className="ui-btn ui-btn-primary"
                disabled={!state.selected}
                onClick={onAccept}
              >
                采纳并移动
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function DocsView({ className = "", onChanged }: Props) {
  const [docs, setDocs] = useState<KBDocument[]>([]);
  const [folders, setFolders] = useState<KBFolder[]>([]);
  const [activeFolder, setActiveFolder] = useState("");
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [treeQuery, setTreeQuery] = useState("");
  const [pending, setPending] = useState<PendingItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [batchProfile, setBatchProfile] = useState("");
  const [aiAvailable, setAiAvailable] = useState(false);
  const [vectorMode, setVectorMode] = useState<EmbeddingStatus | null>(null);
  const [ingestProgress, setIngestProgress] = useState<{
    percent: number;
    current: number;
    total: number;
    message: string;
    filename: string;
  } | null>(null);
  const [ingesting, setIngesting] = useState(false);
  const [batchAi, setBatchAi] = useState<BatchAiState | null>(null);
  const [drag, setDrag] = useState<{ ids: string[]; source: string } | null>(null);
  const [dropTarget, setDropTarget] = useState<{ path: string; valid: boolean } | null>(null);
  const [confirmMove, setConfirmMove] = useState<ConfirmMove | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState("");
  const [undo, setUndo] = useState<{ opId: string; count: number; target: string } | null>(null);
  const [picker, setPicker] = useState<{
    title: string;
    value: string;
    onPick: (path: string) => void;
  } | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [ai, setAi] = useState<{
    docId: string;
    filename: string;
    loading: boolean;
    stage: number;
    result?: ClassificationResult;
    selected: string;
    adjusted: boolean;
    error?: string;
  } | null>(null);
  const expandTimer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const folderList = await fetchFolders();
      setFolders(folderList);
      setActiveFolder((current) =>
        current && folderList.some((f) => f.path === current) ? current : "",
      );
      const docList = await fetchDocuments(activeFolder || null);
      setDocs(docList);
      setSelected((current) => new Set([...current].filter((id) => docList.some((d) => d.docId === id))));
      onChanged?.();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "加载失败");
    }
  }, [activeFolder, onChanged]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadHealth = useCallback(() => {
    fetchHealth()
      .then((info) => setVectorMode(info.embedding))
      .catch(() => setVectorMode(null));
  }, []);

  useEffect(() => {
    fetchModels()
      .then((list) => {
        setModels(list);
        setBatchProfile(list[0]?.id ?? "");
        setAiAvailable(list.length > 0);
      })
      .catch(() => {
        setModels([]);
        setAiAvailable(false);
      });
    loadHealth();
    const timer = window.setInterval(loadHealth, 30000);
    return () => window.clearInterval(timer);
  }, [loadHealth]);

  useEffect(() => {
    if (!undo) return;
    const timer = window.setTimeout(() => setUndo(null), 8000);
    return () => window.clearTimeout(timer);
  }, [undo]);

  useEffect(() => {
    if (!ai?.loading) return;
    const timer = window.setInterval(() => {
      setAi((current) =>
        current && current.loading ? { ...current, stage: Math.min(3, current.stage + 1) } : current,
      );
    }, 700);
    return () => window.clearInterval(timer);
  }, [ai?.loading]);

  useEffect(
    () => () => {
      if (expandTimer.current !== null) window.clearTimeout(expandTimer.current);
    },
    [],
  );

  async function onSelectFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setMessage("");
    for (const file of Array.from(files)) {
      try {
        const report = await previewDocument(file, aiAvailable);
        setPending((p) => [
          ...p,
          {
            file,
            report,
            target: activeFolder || DEFAULT_FOLDER,
            showAi: false,
          },
        ]);
      } catch (e) {
        setMessage(`${file.name}: ${e instanceof Error ? e.message : "预检失败"}`);
      }
    }
    setUploading(false);
  }

  async function ingestOne(item: PendingItem, current: number, total: number): Promise<IngestJob> {
    setIngestProgress({
      percent: Math.round(((current - 1) / total) * 100),
      current,
      total,
      message: `正在入库 ${item.file.name}`,
      filename: item.file.name,
    });
    const started = await startIngestJob(item.file, item.target);
    for (;;) {
      await sleep(400);
      const job = await fetchIngestJob(started.jobId);
      const overall = Math.min(
        100,
        Math.round(((current - 1 + job.percent / 100) / total) * 100),
      );
      setIngestProgress({
        percent: overall,
        current,
        total,
        message: job.message,
        filename: item.file.name,
      });
      if (job.status === "done") return job;
      if (job.status === "error") throw new Error(job.error || job.message || "入库失败");
    }
  }

  async function confirmIngest(item: PendingItem) {
    if (ingesting) return;
    setIngesting(true);
    try {
      const job = await ingestOne(item, 1, 1);
      setPending((p) => p.filter((x) => x.file !== item.file));
      const result = job.result;
      setMessage(
        `${result?.filename ?? item.file.name}: ${
          result?.duplicated ? "内容已存在，跳过" : `入库 ${result?.chunks ?? 0} 块`
        } · ${result?.folder ?? item.target}`,
      );
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "入库失败");
    } finally {
      setIngesting(false);
      setIngestProgress(null);
    }
  }

  async function ingestAll() {
    if (ingesting || pending.length === 0) return;
    setIngesting(true);
    const items = [...pending];
    let done = 0;
    let errorText = "";
    for (let index = 0; index < items.length; index += 1) {
      const item = items[index];
      try {
        await ingestOne(item, index + 1, items.length);
        setPending((p) => p.filter((x) => x.file !== item.file));
        done += 1;
      } catch (e) {
        errorText = `${item.file.name}: ${e instanceof Error ? e.message : "入库失败"}`;
        break;
      }
    }
    setMessage(
      errorText
        ? `已完成 ${done}/${items.length} 份文档入库；${errorText}`
        : `已完成 ${done}/${items.length} 份文档入库`,
    );
    setIngesting(false);
    setIngestProgress(null);
    await refresh();
  }

  async function onDelete(docId: string, filename: string) {
    if (!window.confirm(`删除文档 ${filename} 及其索引？`)) return;
    try {
      await deleteDocument(docId);
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "删除失败");
    }
  }

  async function onCreateFolder(path: string) {
    try {
      await createFolder(path);
      await refresh();
      setActiveFolder(path);
      setMessage(`已创建目录 ${path}`);
      return path;
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "创建失败");
      throw e;
    }
  }

  async function onRenameFolder() {
    if (!activeFolder) return;
    const target = window.prompt("新的目录路径", activeFolder)?.trim();
    if (!target || target === activeFolder) return;
    try {
      await renameFolder(activeFolder, target);
      await refresh();
      setActiveFolder(target);
      setMessage(`已重命名为 ${target}`);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "重命名失败");
    }
  }

  async function onDeleteFolder() {
    if (!activeFolder) return;
    if (!window.confirm(`删除目录「${activeFolder}」？其中文档将移动到上级目录。`)) return;
    try {
      await deleteFolder(activeFolder, "move");
      await refresh();
      setActiveFolder("");
      setMessage("目录已删除，文档已移动到上级目录");
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "删除失败");
    }
  }

  function startDrag(event: DragEvent, doc: KBDocument) {
    const ids = selected.has(doc.docId) ? [...selected] : [doc.docId];
    setDrag({ ids, source: doc.folder || DEFAULT_FOLDER });
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", ids.join(","));
    const ghost = document.createElement("div");
    ghost.className = "ui-drag-ghost";
    ghost.textContent = ids.length > 1 ? `${ids.length} 份文档` : doc.filename;
    document.body.appendChild(ghost);
    event.dataTransfer.setDragImage(ghost, 12, 12);
    window.setTimeout(() => ghost.remove(), 0);
  }

  function endDrag() {
    setDrag(null);
    setDropTarget(null);
    if (expandTimer.current !== null) {
      window.clearTimeout(expandTimer.current);
      expandTimer.current = null;
    }
  }

  function onFolderDragOver(event: DragEvent, path: string) {
    if (!drag) return;
    event.preventDefault();
    event.stopPropagation();
    const valid = drag.source !== path;
    setDropTarget({ path, valid });
    if (valid && collapsed.has(path) && hasChildren(path) && expandTimer.current === null) {
      expandTimer.current = window.setTimeout(() => {
        setCollapsed((current) => {
          const next = new Set(current);
          next.delete(path);
          return next;
        });
        expandTimer.current = null;
      }, 600);
    }
  }

  function onFolderDrop(event: DragEvent, path: string) {
    if (!drag) return;
    event.preventDefault();
    event.stopPropagation();
    if (drag.source !== path) {
      setConfirmMove({ ids: drag.ids, source: drag.source, target: path });
      setConfirmError("");
    }
    endDrag();
  }

  function hasChildren(path: string) {
    return folders.some((f) => f.path.startsWith(path + "/"));
  }

  async function submitMove() {
    if (!confirmMove) return;
    setConfirmBusy(true);
    setConfirmError("");
    try {
      const result = await batchMoveDocuments(confirmMove.ids, confirmMove.target);
      const feedback = confirmMove.suggested && confirmMove.ids.length === 1
        ? {
            docId: confirmMove.ids[0],
            suggested: confirmMove.suggested,
            chosen: confirmMove.target,
            action:
              confirmMove.suggested === confirmMove.target
                ? ("accept" as const)
                : ("adjust" as const),
            source: "doc",
          }
        : null;
      setConfirmMove(null);
      setSelected(new Set());
      if (result.operationId) {
        setUndo({ opId: result.operationId, count: result.moved.length, target: confirmMove.target });
      }
      const skipped = result.skippedDetails?.length ?? result.skipped.length;
      const failed = result.failed.length;
      setMessage(
        `成功移动 ${result.moved.length} 份${
          skipped ? `，跳过 ${skipped} 份（已在目标目录）` : ""
        }${failed ? `，失败 ${failed} 份` : ""}`,
      );
      if (feedback) {
        // 反馈失败不影响已完成的移动
        void sendClassifyFeedback(feedback).catch(() => undefined);
      }
      await refresh();
    } catch (e) {
      setConfirmError(e instanceof Error ? e.message : "移动失败");
    } finally {
      setConfirmBusy(false);
    }
  }

  async function onUndo() {
    if (!undo) return;
    const current = undo;
    setUndo(null);
    try {
      await undoMove(current.opId);
      setMessage("已撤销移动");
      await refresh();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "撤销失败，请手动移回");
    }
  }

  function openAi(doc: KBDocument) {
    if (!aiAvailable) return;
    setAi({
      docId: doc.docId,
      filename: doc.filename,
      loading: true,
      stage: 0,
      selected: "",
      adjusted: false,
    });
    classifyDocument(doc.docId)
      .then((result) => {
        setAi((current) =>
          current
            ? {
                ...current,
                loading: false,
                result,
                selected: result.candidates?.[0]?.path ?? "",
              }
            : current,
        );
      })
      .catch((e) => {
        setAi((current) =>
          current ? { ...current, loading: false, error: e instanceof Error ? e.message : "分析失败" } : current,
        );
      });
  }

  function retryAi() {
    const doc = docs.find((d) => d.docId === ai?.docId);
    if (doc) openAi(doc);
  }

  function rejectAi() {
    if (!ai) return;
    void sendClassifyFeedback({
      docId: ai.docId,
      suggested: ai.result?.candidates?.[0]?.path ?? null,
      action: "reject",
      source: "doc",
    });
    setMessage("已驳回 AI 推荐");
    setAi(null);
  }

  function acceptAi() {
    if (!ai || !ai.selected) return;
    const doc = docs.find((d) => d.docId === ai.docId);
    if (!doc) return;
    setConfirmMove({
      ids: [doc.docId],
      source: doc.folder || DEFAULT_FOLDER,
      target: ai.selected,
      suggested: ai.result?.candidates?.[0]?.path,
    });
    setConfirmError("");
    setAi(null);
  }

  function openBatchAi() {
    if (!aiAvailable || ingesting || batchAi?.phase === "running") return;
    void fetchDocuments(null)
      .then((allDocs) => {
        if (allDocs.length === 0) {
          setMessage("知识库暂无文档，无法执行批量分类");
          return;
        }
        setBatchAi({
          open: true,
          phase: "confirm",
          total: allDocs.length,
          current: 0,
          percent: 0,
          message: "等待开始",
        });
      })
      .catch((e) => setMessage(e instanceof Error ? e.message : "读取文档失败"));
  }

  async function runBatchAi() {
    if (!batchAi || batchAi.phase === "running") return;
    let snapshot: KBDocument[];
    try {
      snapshot = await fetchDocuments(null);
    } catch (e) {
      setBatchAi(null);
      setMessage(e instanceof Error ? e.message : "读取文档失败");
      return;
    }
    const total = snapshot.length;
    if (total === 0) {
      setBatchAi(null);
      setMessage("知识库暂无文档，无法执行批量分类");
      return;
    }
    setBatchAi({
      open: true,
      phase: "running",
      total,
      current: 0,
      percent: 0,
      message: "正在分析文档",
    });
    const groups = new Map<string, string[]>();
    let low = 0;
    let failed = 0;
    let skipped = 0;
    let moved = 0;

    for (let index = 0; index < snapshot.length; index += 1) {
      const doc = snapshot[index];
      setBatchAi((current) =>
        current
          ? {
              ...current,
              current: index + 1,
              percent: Math.round((index / total) * 80),
              message: `正在分析 ${doc.filename}`,
            }
          : current,
      );
      try {
        const result = await classifyDocument(doc.docId, batchProfile || null);
        const top = result.candidates?.find((item) => item.confidence >= 0.6);
        if (!top) {
          low += 1;
          continue;
        }
        if (top.path === (doc.folder || DEFAULT_FOLDER)) {
          skipped += 1;
          continue;
        }
        const ids = groups.get(top.path) ?? [];
        ids.push(doc.docId);
        groups.set(top.path, ids);
      } catch {
        failed += 1;
      }
    }

    let groupIndex = 0;
    for (const [target, ids] of groups) {
      groupIndex += 1;
      setBatchAi((current) =>
        current
          ? {
              ...current,
              percent: 80 + Math.round((groupIndex / Math.max(groups.size, 1)) * 20),
              message: `应用分类 ${groupIndex}/${groups.size}：${target}`,
            }
          : current,
      );
      try {
        const result = await batchMoveDocuments(ids, target);
        moved += result.moved.length;
        skipped += result.skipped.length;
        failed += result.failed.length;
      } catch {
        failed += ids.length;
      }
    }

    setBatchAi({
      open: true,
      phase: "done",
      total,
      current: total,
      percent: 100,
      message: `分类完成：成功移动 ${moved} 篇 · 跳过 ${skipped} 篇 · 低置信度 ${low} 篇 · 失败 ${failed} 篇`,
      result: { moved, skipped, low, failed },
    });
    await refresh();
  }

  const visibleFolders = useMemo(() => {
    const term = treeQuery.trim().toLowerCase();
    if (!term) {
      return folders.filter((f) => {
        const parts = f.path.split("/");
        for (let i = 1; i < parts.length; i += 1) {
          if (collapsed.has(parts.slice(0, i).join("/"))) return false;
        }
        return true;
      });
    }
    const matched = folders.filter((f) => f.path.toLowerCase().includes(term));
    const keep = new Set<string>();
    for (const item of matched) {
      const parts = item.path.split("/");
      for (let i = 1; i <= parts.length; i += 1) keep.add(parts.slice(0, i).join("/"));
    }
    return folders.filter((f) => keep.has(f.path));
  }, [folders, collapsed, treeQuery]);

  const filtered = useMemo(
    () => docs.filter((d) => d.filename.toLowerCase().includes(query.toLowerCase())),
    [docs, query],
  );
  const totalChunks = docs.reduce((n, d) => n + d.chunks, 0);
  const totalDocCount = folders
    .filter((item) => !item.parent)
    .reduce((sum, item) => sum + item.documentCount, 0);
  const allSelected = filtered.length > 0 && filtered.every((d) => selected.has(d.docId));

  function toggleSelect(docId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });
  }

  function toggleSelectAll() {
    setSelected((current) => {
      if (allSelected) return new Set();
      const next = new Set(current);
      filtered.forEach((d) => next.add(d.docId));
      return next;
    });
  }

  return (
    <section
      aria-label="知识库文档"
      aria-busy={uploading}
      className={`ui-docs-view min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6 ${className}`}
    >
      <div className="mx-auto max-w-6xl space-y-5">
        <div className="ui-card flex flex-wrap items-center justify-between gap-4 p-5 sm:p-6">
          <div className="min-w-0">
            <div className="ui-kicker">Knowledge Base</div>
            <h2 className="mt-1 text-xl font-bold tracking-tight text-[var(--ink)] sm:text-2xl">
              知识库文档
            </h2>
            <p className="mt-2 max-w-xl text-sm leading-6 text-[var(--muted)]">
              按多级目录归档文档；拖动文档到左侧目录即可移动，支持批量选择与撤销。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="ui-vector-badge"
              title={`向量化模式：${vectorMode?.mode ?? "auto"}`}
            >
              向量化：{vectorLabel(vectorMode)}
            </span>
            <label
              htmlFor="file-upload"
              aria-disabled={uploading || ingesting}
              className="ui-upload px-4 py-2 text-xs"
            >
              {uploading ? "预检中…" : "上传文档"}
              <input
                id="file-upload"
                type="file"
                multiple
                accept=".pdf,.md,.txt"
                className="sr-only"
                disabled={uploading || ingesting}
                onChange={(e) => void onSelectFiles(e.target.files)}
                aria-label="选择要上传的文档"
              />
            </label>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <div className="ui-stat">
            <div className="ui-stat-value">{docs.length}</div>
            <div className="ui-stat-label">{activeFolder ? "当前目录文档" : "已入库文档"}</div>
          </div>
          <div className="ui-stat">
            <div className="ui-stat-value">{totalChunks}</div>
            <div className="ui-stat-label">检索子块</div>
          </div>
          <div className="ui-stat">
            <div className="ui-stat-value">{pending.length}</div>
            <div className="ui-stat-label">待确认预检</div>
          </div>
        </div>

        {(message || ingestProgress) && (
          <div role="status" aria-live="polite" className="ui-card px-4 py-3 text-xs text-[var(--muted)]">
            {message && <div>{message}</div>}
            {ingestProgress && (
              <div className="ui-progress" aria-label="索引构建进度">
                <div className="ui-progress-head">
                  <span>{ingestProgress.message}</span>
                  <span>{ingestProgress.percent}%</span>
                </div>
                <div className="ui-progress-track">
                  <div
                    className="ui-progress-fill"
                    style={{ width: `${ingestProgress.percent}%` }}
                  />
                </div>
                <div className="ui-progress-meta">
                  文档 {ingestProgress.current}/{ingestProgress.total} · {ingestProgress.filename}
                </div>
              </div>
            )}
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-[260px,minmax(0,1fr)]">
          <aside
            className={`ui-folder-panel p-3 ${drag ? "is-dropzone" : ""}`}
            aria-label="知识库目录"
            onDragOver={(e) => {
              if (drag) e.preventDefault();
            }}
            onDrop={(e) => {
              if (drag) e.preventDefault();
              endDrag();
            }}
          >
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-xs font-bold text-[var(--muted)]">
                {drag ? "拖到目录中以移动" : "目录"}
              </span>
              <button
                type="button"
                onClick={() => {
                  const suggestion = activeFolder ? `${activeFolder}/新目录` : "新目录";
                  const path = window.prompt("新目录路径（用 / 分隔多级）", suggestion)?.trim();
                  if (path) void onCreateFolder(path);
                }}
                className="ui-btn ui-btn-sm ui-btn-ghost"
              >
                + 新建
              </button>
            </div>
            <input
              type="search"
              className="ui-search mb-2 w-full px-2 py-1.5 text-xs outline-none"
              placeholder="搜索目录"
              value={treeQuery}
              onChange={(e) => setTreeQuery(e.target.value)}
              aria-label="搜索目录"
            />
            <button
              type="button"
              onClick={() => setActiveFolder("")}
              className={`ui-folder-item ${activeFolder === "" ? "is-active" : ""}`}
            >
              全部文档
            </button>
            <ul className="mt-1 space-y-0.5">
              {visibleFolders.map((f) => {
                const depth = f.path.split("/").length - 1;
                const canExpand = hasChildren(f.path);
                const isCollapsed = collapsed.has(f.path);
                const target = dropTarget?.path === f.path ? dropTarget : null;
                return (
                  <li key={f.path}>
                    <div
                      onDragOver={(e) => onFolderDragOver(e, f.path)}
                      onDragLeave={() =>
                        setDropTarget((current) => (current?.path === f.path ? null : current))
                      }
                      onDrop={(e) => onFolderDrop(e, f.path)}
                      className={`ui-folder-row ${
                        target ? (target.valid ? "is-drop-target" : "is-drop-invalid") : ""
                      }`}
                    >
                      <button
                        type="button"
                        className="ui-folder-arrow"
                        aria-label={isCollapsed ? `展开 ${f.name}` : `折叠 ${f.name}`}
                        onClick={() =>
                          setCollapsed((current) => {
                            const next = new Set(current);
                            if (next.has(f.path)) next.delete(f.path);
                            else next.add(f.path);
                            return next;
                          })
                        }
                      >
                        {canExpand ? (isCollapsed ? "▸" : "▾") : ""}
                      </button>
                      <button
                        type="button"
                        onClick={() => setActiveFolder(f.path)}
                        style={{ paddingLeft: 4 + depth * 12 }}
                        className={`ui-folder-item ${activeFolder === f.path ? "is-active" : ""}`}
                        title={`${f.path} · ${f.documentCount} 份文档 / ${f.chunkCount} 块`}
                      >
                        <span className="truncate">{f.name}</span>
                        <span className="ui-folder-count">{f.documentCount}</span>
                      </button>
                      {target?.valid && <span className="ui-drop-chip">移入</span>}
                    </div>
                  </li>
                );
              })}
              {visibleFolders.length === 0 && (
                <li className="px-2 py-3 text-xs text-[var(--muted)]">
                  {treeQuery ? "没有匹配的目录" : "还没有目录，上传文档后自动创建"}
                </li>
              )}
            </ul>
            {activeFolder && (
              <div className="mt-3 flex gap-2 border-t border-[var(--line)] pt-3">
                <button type="button" onClick={() => void onRenameFolder()} className="ui-btn ui-btn-sm ui-btn-ghost">
                  重命名
                </button>
                <button type="button" onClick={() => void onDeleteFolder()} className="ui-btn ui-btn-sm ui-btn-danger">
                  删除
                </button>
              </div>
            )}
          </aside>

          <div className="space-y-4">
            {selected.size > 0 && (
              <div className="ui-batch-bar">
                <span>已选 {selected.size} 项</span>
                <span className="flex-1" />
                <button
                  type="button"
                  className="ui-btn ui-btn-sm ui-btn-primary"
                  onClick={() => {
                    const sources = new Set(
                      [...selected].map(
                        (id) => docs.find((d) => d.docId === id)?.folder || DEFAULT_FOLDER,
                      ),
                    );
                    setPicker({
                      title: "批量移动文档",
                      value: activeFolder,
                      onPick: (path) => {
                        setConfirmMove({
                          ids: [...selected],
                          source: sources.size === 1 ? [...sources][0] : "多个目录",
                          target: path,
                        });
                        setConfirmError("");
                        setPicker(null);
                      },
                    });
                  }}
                >
                  移动到…
                </button>
                <button type="button" className="ui-btn ui-btn-sm ui-btn-ghost" onClick={() => setSelected(new Set())}>
                  清除
                </button>
              </div>
            )}

            {pending.length > 1 && (
              <div className="ui-batch-bar">
                <span>待入库 {pending.length} 份文档</span>
                <span className="flex-1" />
                <button
                  type="button"
                  className="ui-btn ui-btn-sm ui-btn-primary"
                  disabled={ingesting}
                  onClick={() => void ingestAll()}
                >
                  {ingesting ? "入库中…" : "全部入库"}
                </button>
              </div>
            )}

            {pending.map((item) => {
              const classification = item.report.classification;
              const candidates = classification?.candidates ?? [];
              const noModel = !aiAvailable || classification?.available === false;
              return (
                <div key={`${item.file.name}-${item.file.lastModified}`} className="ui-pending-card p-4 sm:p-5">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="min-w-0 flex items-center gap-2">
                      <span className="truncate text-sm font-bold text-[var(--ink)]">{item.report.filename}</span>
                      <span
                        className="ui-status px-1.5 py-0.5"
                        data-tone={item.report.status === "ok" ? "ok" : item.report.status === "warning" ? "warning" : "error"}
                      >
                        {item.report.status === "ok" ? "预检通过" : item.report.status === "warning" ? "有警示" : "不建议入库"}
                      </span>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      {item.report.status !== "rejected" && (
                        <button type="button" onClick={() => void confirmIngest(item)} disabled={ingesting} className="ui-btn ui-btn-primary">
                          {ingesting ? "入库中…" : "确认入库"}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setPending((p) => p.filter((x) => x.file !== item.file))}
                        className="ui-btn ui-btn-ghost"
                      >
                        {item.report.status === "rejected" ? "移除" : "放弃"}
                      </button>
                    </div>
                  </div>
                  <div className="mt-3 text-xs leading-5 text-[var(--muted)]">
                    {item.report.pages ?? "-"} 页 · 预计 {item.report.estimatedChunks} 块 · {item.report.tables} 个表格 ·{" "}
                    {item.report.language === "zh" ? "中文" : "英文"}
                    {item.report.duplicated && " · 内容已存在（将自动跳过）"}
                  </div>
                  <div className="ui-ingest-path mt-3">
                    <span className="ui-ingest-label">入库目录</span>
                    <span className="ui-ingest-value">{item.target}</span>
                    <button
                      type="button"
                      className="ui-btn ui-btn-sm ui-btn-ghost"
                      onClick={() =>
                        setPicker({
                          title: "选择入库目录",
                          value: item.target,
                          onPick: (path) => {
                            setPending((p) => p.map((x) => (x.file === item.file ? { ...x, target: path } : x)));
                            setPicker(null);
                          },
                        })
                      }
                    >
                      更改
                    </button>
                    <button
                      type="button"
                      className="ui-btn ui-btn-sm ui-btn-ghost"
                      disabled={noModel}
                      title={noModel ? "无可用模型" : "查看 AI 推荐目录"}
                      onClick={() =>
                        setPending((p) =>
                          p.map((x) => (x.file === item.file ? { ...x, showAi: !x.showAi } : x)),
                        )
                      }
                    >
                      AI 推荐
                    </button>
                  </div>
                  {item.showAi && (
                    <div className="ui-ingest-ai">
                      {candidates.length === 0 ? (
                        <p className="text-xs text-[var(--muted)]">暂无可靠推荐，请手动选择目录。</p>
                      ) : (
                        candidates.map((c) => (
                          <label
                            key={c.path}
                            className={`ui-ai-candidate ${item.target === c.path ? "is-active" : ""}`}
                          >
                            <input
                              type="radio"
                              name={`ingest-${item.file.name}`}
                              checked={item.target === c.path}
                              onChange={() =>
                                setPending((p) =>
                                  p.map((x) => (x.file === item.file ? { ...x, target: c.path } : x)),
                                )
                              }
                            />
                            <span className="ui-ai-candidate-main">
                              <span className="ui-ai-path">{c.path}</span>
                              <span className="ui-ai-reason">{c.reason}</span>
                            </span>
                            <ConfidenceBadge level={c.level} value={c.confidence} />
                          </label>
                        ))
                      )}
                      {classification?.fallback && (
                        <p className="text-[10px] text-[var(--warning)]">AI 响应超时，已使用规则推荐。</p>
                      )}
                    </div>
                  )}
                  {item.report.issues.map((issue, i) => (
                    <div
                      key={i}
                      className={`mt-1 text-xs ${
                        issue.level === "error"
                          ? "text-[var(--danger)]"
                          : issue.level === "warning"
                            ? "text-[var(--warning)]"
                            : "text-[var(--muted)]"
                      }`}
                    >
                      {issue.message}
                      {issue.hint && `（${issue.hint}）`}
                    </div>
                  ))}
                </div>
              );
            })}

            <div className="ui-card flex flex-wrap items-center gap-2 p-3">
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="按文件名筛选"
                aria-label="按文件名筛选"
                className="ui-search w-full min-w-0 px-3 py-2 text-xs outline-none sm:w-72"
              />
              <button type="button" onClick={() => void refresh()} aria-label="刷新文档列表" className="ui-btn ui-btn-ghost">
                刷新
              </button>
              <span className="flex-1" />
              <ModelPicker
                models={models}
                profile={batchProfile}
                onSelect={setBatchProfile}
                disabled={!aiAvailable || batchAi?.phase === "running" || ingesting}
              />
              <button
                type="button"
                className="ui-btn ui-btn-primary"
                disabled={!aiAvailable || totalDocCount === 0 || batchAi?.phase === "running" || ingesting}
                title={!aiAvailable ? "无可用模型" : "对知识库全部文档执行 AI 分类"}
                onClick={openBatchAi}
              >
                AI 批量分类
              </button>
            </div>

            <div className="ui-table-wrap">
              <table className="ui-table">
                <caption className="sr-only">已入库文档列表</caption>
                <thead>
                  <tr>
                    <th scope="col" className="px-3 py-3">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleSelectAll}
                        aria-label="全选当前列表"
                      />
                    </th>
                    <th scope="col" className="px-4 py-3">文件名</th>
                    <th scope="col" className="px-4 py-3">目录</th>
                    <th scope="col" className="px-4 py-3">子块</th>
                    <th scope="col" className="px-4 py-3">页/表</th>
                    <th scope="col" className="px-4 py-3">入库时间</th>
                    <th scope="col" className="px-4 py-3"><span className="sr-only">操作</span></th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((d) => (
                    <tr
                      key={d.docId}
                      draggable
                      onDragStart={(e) => startDrag(e, d)}
                      onDragEnd={endDrag}
                      className={drag?.ids.includes(d.docId) ? "is-dragging" : ""}
                    >
                      <td className="px-3 py-3">
                        <input
                          type="checkbox"
                          checked={selected.has(d.docId)}
                          onChange={() => toggleSelect(d.docId)}
                          aria-label={`选择 ${d.filename}`}
                        />
                      </td>
                      <td className="max-w-[20rem] truncate px-4 py-3">{d.filename}</td>
                      <td className="px-4 py-3 text-xs text-[var(--muted)]">{d.folder ?? DEFAULT_FOLDER}</td>
                      <td className="px-4 py-3 text-[var(--muted)]">{d.chunks}</td>
                      <td className="px-4 py-3 text-xs text-[var(--muted)]">{d.pages ?? "-"} / {d.tables ?? "-"}</td>
                      <td className="px-4 py-3 text-xs text-[var(--muted)]">{d.ingestedAt ?? "-"}</td>
                      <td className="px-4 py-3">
                        <div className="ui-row-actions">
                          <button
                            type="button"
                            disabled={!aiAvailable}
                            title={aiAvailable ? "AI 分类" : "无可用模型"}
                            onClick={() => openAi(d)}
                            className="ui-btn ui-btn-sm ui-btn-ghost"
                          >
                            AI
                          </button>
                          <button
                            type="button"
                            onClick={() =>
                              setPicker({
                                title: `移动 ${d.filename}`,
                                value: d.folder ?? DEFAULT_FOLDER,
                                onPick: (path) => {
                                  setConfirmMove({
                                    ids: [d.docId],
                                    source: d.folder || DEFAULT_FOLDER,
                                    target: path,
                                  });
                                  setConfirmError("");
                                  setPicker(null);
                                },
                              })
                            }
                            className="ui-btn ui-btn-sm ui-btn-ghost"
                          >
                            移动
                          </button>
                          <button
                            type="button"
                            onClick={() => void onDelete(d.docId, d.filename)}
                            aria-label={`删除文档 ${d.filename}`}
                            className="ui-btn ui-btn-sm ui-btn-danger"
                          >
                            删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {filtered.length === 0 && (
                    <tr>
                      <td colSpan={7} className="px-4 py-12 text-center text-sm text-[var(--muted)]">
                        {docs.length === 0
                          ? activeFolder
                            ? "该目录下还没有文档"
                            : "知识库为空，上传 PDF / Markdown / TXT 后即可提问"
                          : "没有匹配的文档"}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      {confirmMove && (
        <ConfirmDialog
          move={confirmMove}
          docs={docs}
          busy={confirmBusy}
          error={confirmError}
          onCancel={() => {
            if (!confirmBusy) setConfirmMove(null);
          }}
          onConfirm={() => void submitMove()}
        />
      )}

      {ai && (
        <AiPanel
          state={ai}
          onClose={() => setAi(null)}
          onRetry={retryAi}
          onSelect={(path) =>
            setAi((current) => (current ? { ...current, selected: path } : current))
          }
          onAdjust={() =>
            setPicker({
              title: "选择目标目录",
              value: ai.selected || ai.result?.candidates?.[0]?.path || "",
              onPick: (path) => {
                setAi((current) => (current ? { ...current, selected: path, adjusted: true } : current));
                setPicker(null);
              },
            })
          }
          onReject={rejectAi}
          onAccept={acceptAi}
        />
      )}

      {picker && (
        <PathPicker
          title={picker.title}
          value={picker.value}
          folders={folders}
          onClose={() => setPicker(null)}
          onPick={picker.onPick}
          onCreate={async (path) => {
            try {
              await onCreateFolder(path);
              picker.onPick(path);
            } catch {
              /* 错误已写入页面提示 */
            }
          }}
        />
      )}

      {batchAi?.open && (
        <div
          className="ui-modal-backdrop"
          role="presentation"
          onClick={batchAi.phase === "running" ? undefined : () => setBatchAi(null)}
        >
          <div
            className="ui-modal ui-ai-panel"
            role="dialog"
            aria-modal="true"
            aria-label="AI 批量分类"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="ui-modal-head">
              <h3>AI 批量分类</h3>
              {batchAi.phase !== "running" && (
                <button
                  type="button"
                  className="ui-btn ui-btn-sm ui-btn-ghost"
                  onClick={() => setBatchAi(null)}
                  aria-label="关闭"
                >
                  ×
                </button>
              )}
            </div>

            {batchAi.phase === "confirm" && (
              <>
                <p className="ui-ai-doc">
                  将对知识库全部 <strong>{batchAi.total}</strong> 篇文档执行 AI 分类。
                </p>
                <p className="ui-confirm-hint">
                  仅更新文档目录元数据，<strong>不重建向量索引</strong>；向量数量与内容保持不变。
                </p>
                <p className="ui-confirm-hint">置信度低于 60% 的文档保持不变。</p>
                <div className="ui-modal-actions">
                  <button type="button" className="ui-btn ui-btn-ghost" onClick={() => setBatchAi(null)}>
                    取消
                  </button>
                  <button type="button" className="ui-btn ui-btn-primary" onClick={() => void runBatchAi()}>
                    开始分类
                  </button>
                </div>
              </>
            )}

            {batchAi.phase === "running" && (
              <>
                <div className="ui-progress">
                  <div className="ui-progress-head">
                    <span>{batchAi.message}</span>
                    <span>{batchAi.percent}%</span>
                  </div>
                  <div className="ui-progress-track">
                    <div className="ui-progress-fill" style={{ width: `${batchAi.percent}%` }} />
                  </div>
                  <div className="ui-progress-meta">
                    文档 {batchAi.current}/{batchAi.total}
                  </div>
                </div>
                <p className="ui-confirm-hint">
                  正在逐篇分析并应用分类；仅更新目录元数据，不重建向量索引，请勿关闭页面。
                </p>
              </>
            )}

            {batchAi.phase === "done" && batchAi.result && (
              <>
                <p className="ui-batch-summary" role="status">
                  {batchAi.message}
                </p>
                <div className="ui-batch-result">
                  <p>成功移动：<strong>{batchAi.result.moved}</strong> 篇</p>
                  <p>跳过：{batchAi.result.skipped} 篇（已在目标目录）</p>
                  <p>低置信度未分类：{batchAi.result.low} 篇</p>
                  <p>失败：{batchAi.result.failed} 篇</p>
                </div>
                <p className="ui-confirm-hint">本次仅更新目录元数据，未重建向量索引。</p>
                <div className="ui-modal-actions">
                  <button type="button" className="ui-btn ui-btn-primary" onClick={() => setBatchAi(null)}>
                    完成
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {undo && (
        <div className="ui-undo-toast" role="status" aria-live="polite">
          <span className="flex-1">
            已移动 {undo.count} 份文档到「{undo.target}」
          </span>
          <button type="button" className="ui-btn ui-btn-sm ui-btn-ghost" onClick={() => void onUndo()}>
            撤销
          </button>
          <button
            type="button"
            className="ui-undo-close"
            onClick={() => setUndo(null)}
            aria-label="关闭撤销提示"
          >
            ×
          </button>
        </div>
      )}
    </section>
  );
}

export default memo(DocsView);
