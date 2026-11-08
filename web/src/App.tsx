import { useEffect, useRef, useState } from "react";
import {
  deleteDocument,
  fetchDocuments,
  previewDocument,
  sendChat,
  uploadDocument,
  type ChatMode,
  type PreviewReport,
} from "./lib/api";
import type { ChatMessage, KBDocument, Source, TraceStep } from "./lib/types";

const CONF_LABEL: Record<string, { text: string; cls: string }> = {
  high: { text: "置信度 高", cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  mid: { text: "置信度 中", cls: "bg-amber-50 text-amber-700 border-amber-200" },
  low: { text: "置信度 低", cls: "bg-rose-50 text-rose-700 border-rose-200" },
};

const STAGE_LABEL: Record<string, string> = {
  classify: "意图分类",
  retrieve: "检索",
  decide: "多跳决策",
};

export default function App() {
  const [tab, setTab] = useState<"chat" | "docs">("chat");
  return (
    <div className="mx-auto flex h-screen max-w-3xl flex-col px-4">
      <header className="flex items-center justify-between border-b border-slate-200 py-4">
        <div>
          <h1 className="text-lg font-semibold text-slate-800">工阅 IndocQA</h1>
          <p className="text-xs text-slate-500">工业技术文档 · 自适应检索问答</p>
        </div>
        <nav className="flex rounded-lg border border-slate-200 bg-white p-0.5 text-sm">
          {(
            [
              ["chat", "问答"],
              ["docs", "文档管理"],
            ] as ["chat" | "docs", string][]
          ).map(([value, label]) => (
            <button
              key={value}
              onClick={() => setTab(value)}
              className={`rounded-md px-3 py-1.5 ${
                tab === value ? "bg-slate-800 text-white" : "text-slate-600 hover:text-slate-900"
              }`}
            >
              {label}
            </button>
          ))}
        </nav>
      </header>
      {tab === "chat" ? <ChatView /> : <DocsView />}
    </div>
  );
}

function ChatView() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<ChatMode>("kb");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function patchLast(fn: (m: ChatMessage) => ChatMessage) {
    setMessages((ms) => ms.map((m, i) => (i === ms.length - 1 ? fn(m) : m)));
  }

  async function ask() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setMessages((ms) => [
      ...ms,
      { role: "user", content: text },
      { role: "assistant", content: "", streaming: true, trace: [] },
    ]);
    await sendChat(text, mode, {
      onSources: (items) => patchLast((m) => ({ ...m, sources: items })),
      onToken: (delta) => patchLast((m) => ({ ...m, content: m.content + delta })),
      onTrace: (step) => patchLast((m) => ({ ...m, trace: [...(m.trace ?? []), step] })),
      onDone: (confidence, reason) =>
        patchLast((m) => ({ ...m, confidence, confidenceReason: reason })),
      onError: (message) =>
        patchLast((m) => ({
          ...m,
          content: `${m.content}${m.content ? "\n" : ""}[请求失败] ${message}`,
        })),
    });
    patchLast((m) => ({ ...m, streaming: false }));
    setBusy(false);
  }

  return (
    <>
      <div className="flex items-center justify-between py-3 text-sm">
        <div className="flex rounded-lg border border-slate-200 bg-white p-0.5">
          {(
            [
              ["kb", "知识库问答"],
              ["direct", "直接对话"],
            ] as [ChatMode, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              onClick={() => setMode(value)}
              className={`rounded-md px-3 py-1.5 ${
                mode === value ? "bg-slate-800 text-white" : "text-slate-600 hover:text-slate-900"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-400">
          {mode === "kb" ? "回答仅依据已入库文档，附引用来源" : "不经过知识库检索"}
        </span>
      </div>

      <main className="flex-1 space-y-6 overflow-y-auto pb-2">
        {messages.length === 0 && (
          <div className="mt-16 text-center text-sm text-slate-400">
            试试：MTBF 和平均无故障时间是同一个指标吗？额定值是多少？
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-slate-800 px-4 py-2.5 text-sm text-white">
                {m.content}
              </div>
            </div>
          ) : (
            <div key={i} className="space-y-2">
              <div className="max-w-[90%] whitespace-pre-wrap rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-2.5 text-sm leading-6 text-slate-800 shadow-sm">
                {m.content || (m.streaming ? "正在检索…" : "")}
                {m.streaming && m.content && (
                  <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-slate-400 align-middle" />
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {m.confidence && CONF_LABEL[m.confidence] && (
                  <span
                    className={`rounded-md border px-2 py-0.5 text-xs ${CONF_LABEL[m.confidence].cls}`}
                    title={m.confidenceReason}
                  >
                    {CONF_LABEL[m.confidence].text}
                  </span>
                )}
                {m.trace && m.trace.length > 0 && <TracePanel steps={m.trace} />}
              </div>
              {m.sources && m.sources.length > 0 && <SourcePanel sources={m.sources} />}
            </div>
          ),
        )}
        <div ref={bottomRef} />
      </main>

      <footer className="border-t border-slate-200 py-4">
        <div className="flex gap-3">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
            placeholder={mode === "kb" ? "例如：空压机报错 E-05 应该怎么处理？" : "输入内容，直接与模型对话"}
            className="flex-1 rounded-lg border border-slate-300 px-4 py-2.5 text-sm outline-none focus:border-slate-500"
          />
          <button
            onClick={ask}
            disabled={busy || !input.trim()}
            className="rounded-lg bg-slate-800 px-5 py-2.5 text-sm font-medium text-white disabled:opacity-40"
          >
            {busy ? "回答中" : "提问"}
          </button>
        </div>
      </footer>
    </>
  );
}

function TracePanel({ steps }: { steps: TraceStep[] }) {
  return (
    <details className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
      <summary className="cursor-pointer select-none text-slate-500">执行链（{steps.length} 步）</summary>
      <ul className="mt-1.5 space-y-1">
        {steps.map((s, i) => (
          <li key={i} className="flex gap-2">
            <span className="w-14 shrink-0 text-slate-400">{STAGE_LABEL[s.stage] ?? s.stage}</span>
            <span className="truncate">
              {s.stage === "classify" && `${s.intent} → ${s.plan}`}
              {s.stage === "retrieve" &&
                `${s.strategy}${s.hop ? ` · 第${s.hop}跳` : ""} · 命中 ${s.hits} 条${
                  s.expansions && s.expansions.length ? ` · 术语扩展: ${s.expansions.join("; ")}` : ""
                }`}
              {s.stage === "decide" && s.result}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function SourcePanel({ sources }: { sources: Source[] }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <div className="mb-2 text-xs font-medium text-slate-500">引用来源（{sources.length}）</div>
      <ul className="space-y-1.5">
        {sources.map((s) => (
          <li key={s.index} className="flex items-baseline gap-2 text-xs text-slate-600">
            <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-slate-300 text-[10px]">
              {s.index}
            </span>
            <span className="truncate">
              {s.docName} · {s.section}
              {s.page != null && ` · 第${s.page}页`}
              {s.locator && ` · ${s.locator}`}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DocsView() {
  const [docs, setDocs] = useState<KBDocument[]>([]);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<{ file: File; report: PreviewReport }[]>([]);

  async function refresh() {
    try {
      setDocs(await fetchDocuments());
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "加载失败");
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

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

  return (
    <main className="flex-1 space-y-4 overflow-y-auto py-6">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-slate-700">知识库文档（{docs.length}）</h2>
        <label
          className={`cursor-pointer rounded-lg bg-slate-800 px-4 py-2 text-xs font-medium text-white ${
            uploading ? "pointer-events-none opacity-50" : ""
          }`}
        >
          {uploading ? "预检中…" : "上传文档（先预检）"}
          <input
            type="file"
            multiple
            accept=".pdf,.docx,.md,.txt"
            className="hidden"
            onChange={(e) => onSelect(e.target.files)}
          />
        </label>
      </div>
      {message && <div className="rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600">{message}</div>}
      {pending.map(({ file, report }) => (
        <div key={file.name + report.estimatedChunks} className="rounded-xl border border-slate-200 bg-white p-4">
          <div className="flex items-center justify-between">
            <div>
              <span className="text-sm font-medium text-slate-800">{report.filename}</span>
              <span
                className={`ml-2 rounded-md border px-1.5 py-0.5 text-xs ${
                  report.status === "ok"
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : report.status === "warning"
                      ? "border-amber-200 bg-amber-50 text-amber-700"
                      : "border-rose-200 bg-rose-50 text-rose-700"
                }`}
              >
                {report.status === "ok" ? "预检通过" : report.status === "warning" ? "有警示" : "不建议入库"}
              </span>
            </div>
            <div className="flex gap-2 text-xs">
              {report.status !== "rejected" && (
                <button
                  onClick={() => void confirmIngest(file)}
                  className="rounded-md bg-slate-800 px-3 py-1.5 font-medium text-white"
                >
                  确认入库
                </button>
              )}
              <button
                onClick={() => setPending((p) => p.filter((x) => x.file !== file))}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-slate-600"
              >
                {report.status === "rejected" ? "移除" : "放弃"}
              </button>
            </div>
          </div>
          <div className="mt-2 text-xs text-slate-500">
            {report.pages ?? "-"} 页 · 预计 {report.estimatedChunks} 块 · {report.tables} 个表格 ·{" "}
            {report.language === "zh" ? "中文" : "英文"}
            {report.duplicated && " · 内容已存在（将自动跳过）"}
          </div>
          {report.issues.map((issue, i) => (
            <div
              key={i}
              className={`mt-1 text-xs ${
                issue.level === "error" ? "text-rose-600" : issue.level === "warning" ? "text-amber-600" : "text-slate-400"
              }`}
            >
              {issue.message}
              {issue.hint && `（${issue.hint}）`}
            </div>
          ))}
        </div>
      ))}
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs text-slate-400">
            <th className="py-2 font-normal">文件名</th>
            <th className="py-2 font-normal">子块</th>
            <th className="py-2 font-normal">入库时间</th>
            <th className="py-2" />
          </tr>
        </thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.docId} className="border-b border-slate-100">
              <td className="max-w-[22rem] truncate py-2.5 text-slate-700">{d.filename}</td>
              <td className="py-2.5 text-slate-500">{d.chunks}</td>
              <td className="py-2.5 text-xs text-slate-400">{d.ingestedAt ?? "-"}</td>
              <td className="py-2.5 text-right">
                <button
                  onClick={() => onDelete(d.docId, d.filename)}
                  className="text-xs text-rose-600 hover:underline"
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
          {docs.length === 0 && (
            <tr>
              <td colSpan={4} className="py-10 text-center text-sm text-slate-400">
                知识库为空，上传 PDF / Word / Markdown 文档后即可提问
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </main>
  );
}
