import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  deleteDocument,
  fetchDocuments,
  previewDocument,
  uploadDocument,
  type PreviewReport,
} from "../lib/api";
import type { KBDocument } from "../lib/types";

interface Props {
  className?: string;
}

function DocsView({ className = "" }: Props) {
  const [docs, setDocs] = useState<KBDocument[]>([]);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<{ file: File; report: PreviewReport }[]>([]);

  const refresh = useCallback(async () => {
    try {
      setDocs(await fetchDocuments());
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "加载失败");
    }
  }, []);

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
    setPending((p) => p.filter((x) => x.file !== file));
    setMessage(`正在入库 ${file.name}…`);
    try {
      const result = await uploadDocument(file);
      setMessage(
        `${result.filename}: ${result.duplicated ? "内容已存在，跳过" : `入库 ${result.chunks} 块`}`,
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

  const filtered = useMemo(
    () => docs.filter((d) => d.filename.toLowerCase().includes(query.toLowerCase())),
    [docs, query],
  );
  const totalChunks = docs.reduce((n, d) => n + d.chunks, 0);

  return (
    <section
      aria-label="知识库文档"
      aria-busy={uploading}
      className={`ui-docs-view min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6 ${className}`}
    >
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="ui-card flex flex-wrap items-center justify-between gap-4 p-5 sm:p-6">
          <div className="min-w-0">
            <div className="ui-kicker">Knowledge Base</div>
            <h2 className="mt-1 text-xl font-bold tracking-tight text-[var(--ink)] sm:text-2xl">
              知识库文档
            </h2>
            <p className="mt-2 max-w-xl text-sm leading-6 text-[var(--muted)]">
              管理已入库文档、查看预检报告与索引规模。上传前会自动做格式、文本层和重复内容检查。
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
            <div className="ui-stat-label">已入库文档</div>
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
              {report.language === "zh" ? "中文" : "英文"}
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
          <button
            type="button"
            onClick={() => void refresh()}
            aria-label="刷新文档列表"
            className="ui-btn ui-btn-ghost"
          >
            刷新
          </button>
        </div>

        <div className="ui-table-wrap">
          <table className="ui-table">
            <caption className="sr-only">已入库文档列表</caption>
            <thead>
              <tr>
                <th scope="col" className="px-4 py-3">
                  文件名
                </th>
                <th scope="col" className="px-4 py-3">
                  子块
                </th>
                <th scope="col" className="px-4 py-3">
                  入库时间
                </th>
                <th scope="col" className="px-4 py-3">
                  <span className="sr-only">操作</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((d) => (
                <tr key={d.docId}>
                  <td className="max-w-[26rem] truncate px-4 py-3">{d.filename}</td>
                  <td className="px-4 py-3 text-[var(--muted)]">{d.chunks}</td>
                  <td className="px-4 py-3 text-xs text-[var(--muted)]">
                    {d.ingestedAt ?? "-"}
                  </td>
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
                  <td colSpan={4} className="px-4 py-12 text-center text-sm text-[var(--muted)]">
                    {docs.length === 0
                      ? "知识库为空，上传 PDF / Markdown / TXT 后即可提问"
                      : "没有匹配的文档"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

export default memo(DocsView);
