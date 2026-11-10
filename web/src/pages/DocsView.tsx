import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  createFolder,
  deleteDocument,
  deleteFolder,
  fetchDocuments,
  fetchFolders,
  moveDocument,
  previewDocument,
  renameFolder,
  uploadDocument,
  type PreviewReport,
} from "../lib/api";
import type { KBDocument, KBFolder } from "../lib/types";

interface Props {
  className?: string;
  onChanged?: () => void;
}

const DEFAULT_FOLDER = "未分类";

function DocsView({ className = "", onChanged }: Props) {
  const [docs, setDocs] = useState<KBDocument[]>([]);
  const [folders, setFolders] = useState<KBFolder[]>([]);
  const [activeFolder, setActiveFolder] = useState("");
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<{ file: File; report: PreviewReport }[]>([]);

  const refresh = useCallback(async () => {
    try {
      const folderList = await fetchFolders();
      setFolders(folderList);
      setActiveFolder((current) =>
        current && folderList.some((f) => f.path === current) ? current : "",
      );
      setDocs(await fetchDocuments(activeFolder || null));
      onChanged?.();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "加载失败");
    }
  }, [activeFolder, onChanged]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onSelect(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setMessage("");
    for (const file of Array.from(files)) {
      try {
        const report = await previewDocument(file);
        setPending((p) => [...p, { file, report }]);
      } catch (e) {
        setMessage(`${file.name}: ${e instanceof Error ? e.message : "预检失败"}`);
      }
    }
    setUploading(false);
  }

  async function confirmIngest(file: File) {
    const target = activeFolder || DEFAULT_FOLDER;
    setPending((p) => p.filter((x) => x.file !== file));
    setMessage(`正在入库 ${file.name} → ${target}…`);
    try {
      const result = await uploadDocument(file, target);
      setMessage(
        `${result.filename}: ${result.duplicated ? "内容已存在，跳过" : `入库 ${result.chunks} 块`} · ${result.folder}`,
      );
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "入库失败");
    }
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

  async function onMove(docId: string, folder: string) {
    try {
      await moveDocument(docId, folder);
      await refresh();
      setMessage("已移动文档，检索范围同步生效");
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "移动失败");
    }
  }

  async function onCreateFolder() {
    const suggestion = activeFolder ? `${activeFolder}/新目录` : "新目录";
    const path = window.prompt("目录路径（用 / 分隔多级）", suggestion)?.trim();
    if (!path) return;
    try {
      await createFolder(path);
      await refresh();
      setActiveFolder(path);
      setMessage(`已创建目录 ${path}`);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "创建失败");
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

  const filtered = useMemo(
    () => docs.filter((d) => d.filename.toLowerCase().includes(query.toLowerCase())),
    [docs, query],
  );
  const totalChunks = docs.reduce((n, d) => n + d.chunks, 0);
  const folderOptions = useMemo(() => folders.map((f) => f.path), [folders]);

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
              按多级目录归档文档，上传前自动做格式、文本层和重复内容检查；问答可限定在某个目录内检索。
            </p>
          </div>
          <label
            htmlFor="file-upload"
            aria-disabled={uploading}
            className="ui-upload px-4 py-2 text-xs"
          >
            {uploading ? "预检中…" : "上传文档"}
            <input
              id="file-upload"
              type="file"
              multiple
              accept=".pdf,.md,.txt"
              className="sr-only"
              disabled={uploading}
              onChange={(e) => void onSelect(e.target.files)}
              aria-label="选择要上传的文档"
            />
          </label>
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

        {message && (
          <div
            role="status"
            aria-live="polite"
            className="ui-card px-4 py-3 text-xs text-[var(--muted)]"
          >
            {message}
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-[240px,minmax(0,1fr)]">
          <aside className="ui-folder-panel p-3" aria-label="知识库目录">
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-xs font-bold text-[var(--muted)]">目录</span>
              <button type="button" onClick={() => void onCreateFolder()} className="ui-btn ui-btn-sm ui-btn-ghost">
                + 新建
              </button>
            </div>
            <button
              type="button"
              onClick={() => setActiveFolder("")}
              className={`ui-folder-item ${activeFolder === "" ? "is-active" : ""}`}
            >
              全部文档
            </button>
            <ul className="mt-1 space-y-0.5">
              {folders.map((f) => {
                const depth = f.path.split("/").length - 1;
                return (
                  <li key={f.path}>
                    <button
                      type="button"
                      onClick={() => setActiveFolder(f.path)}
                      style={{ paddingLeft: 8 + depth * 12 }}
                      className={`ui-folder-item ${activeFolder === f.path ? "is-active" : ""}`}
                      title={`${f.path} · ${f.documentCount} 份文档 / ${f.chunkCount} 块`}
                    >
                      <span className="truncate">{f.name}</span>
                      <span className="ui-folder-count">{f.documentCount}</span>
                    </button>
                  </li>
                );
              })}
              {folders.length === 0 && (
                <li className="px-2 py-3 text-xs text-[var(--muted)]">还没有目录，上传文档后自动创建</li>
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
            {pending.map(({ file, report }) => (
              <div key={`${file.name}-${file.lastModified}`} className="ui-pending-card p-4 sm:p-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0 flex items-center gap-2">
                    <span className="truncate text-sm font-bold text-[var(--ink)]">
                      {report.filename}
                    </span>
                    <span
                      className="ui-status px-1.5 py-0.5"
                      data-tone={
                        report.status === "ok"
                          ? "ok"
                          : report.status === "warning"
                            ? "warning"
                            : "error"
                      }
                    >
                      {report.status === "ok"
                        ? "预检通过"
                        : report.status === "warning"
                          ? "有警示"
                          : "不建议入库"}
                    </span>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {report.status !== "rejected" && (
                      <button
                        type="button"
                        onClick={() => void confirmIngest(file)}
                        className="ui-btn ui-btn-primary"
                      >
                        确认入库
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => setPending((p) => p.filter((x) => x.file !== file))}
                      className="ui-btn ui-btn-ghost"
                    >
                      {report.status === "rejected" ? "移除" : "放弃"}
                    </button>
                  </div>
                </div>
                <div className="mt-3 text-xs leading-5 text-[var(--muted)]">
                  {report.pages ?? "-"} 页 · 预计 {report.estimatedChunks} 块 · {report.tables} 个表格 ·{" "}
                  {report.language === "zh" ? "中文" : "英文"} · 入库到{" "}
                  <strong>{activeFolder || DEFAULT_FOLDER}</strong>
                  {report.duplicated && " · 内容已存在（将自动跳过）"}
                </div>
                {report.issues.map((issue, i) => (
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
            ))}

            <div className="ui-card flex flex-wrap items-center gap-2 p-3">
              <label htmlFor="doc-query" className="sr-only">
                按文件名筛选
              </label>
              <input
                id="doc-query"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="按文件名筛选"
                className="ui-search w-full min-w-0 px-3 py-2 text-xs outline-none sm:w-72"
              />
              <button type="button" onClick={() => void refresh()} aria-label="刷新文档列表" className="ui-btn ui-btn-ghost">
                刷新
              </button>
            </div>

            <div className="ui-table-wrap">
              <table className="ui-table">
                <caption className="sr-only">已入库文档列表</caption>
                <thead>
                  <tr>
                    <th scope="col" className="px-4 py-3">文件名</th>
                    <th scope="col" className="px-4 py-3">目录</th>
                    <th scope="col" className="px-4 py-3">子块</th>
                    <th scope="col" className="px-4 py-3">页/表</th>
                    <th scope="col" className="px-4 py-3">入库时间</th>
                    <th scope="col" className="px-4 py-3">
                      <span className="sr-only">操作</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((d) => (
                    <tr key={d.docId}>
                      <td className="max-w-[22rem] truncate px-4 py-3">{d.filename}</td>
                      <td className="px-4 py-3">
                        <select
                          value={d.folder ?? DEFAULT_FOLDER}
                          onChange={(e) => void onMove(d.docId, e.target.value)}
                          aria-label={`移动 ${d.filename} 到目录`}
                          className="ui-folder-select"
                        >
                          {!folderOptions.includes(d.folder ?? DEFAULT_FOLDER) && (
                            <option value={d.folder ?? DEFAULT_FOLDER}>{d.folder ?? DEFAULT_FOLDER}</option>
                          )}
                          {folderOptions.map((path) => (
                            <option key={path} value={path}>{path}</option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-3 text-[var(--muted)]">{d.chunks}</td>
                      <td className="px-4 py-3 text-xs text-[var(--muted)]">
                        {d.pages ?? "-"} / {d.tables ?? "-"}
                      </td>
                      <td className="px-4 py-3 text-xs text-[var(--muted)]">{d.ingestedAt ?? "-"}</td>
                      <td className="px-4 py-3 text-right">
                        <button
                          type="button"
                          onClick={() => void onDelete(d.docId, d.filename)}
                          aria-label={`删除文档 ${d.filename}`}
                          className="ui-btn ui-btn-sm ui-btn-danger"
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                  {filtered.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-12 text-center text-sm text-[var(--muted)]">
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
    </section>
  );
}

export default memo(DocsView);
