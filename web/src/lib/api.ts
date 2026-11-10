import { postSSE } from "./sse";
import type {
  ChatMode,
  ClassificationResult,
  DonePayload,
  HealthInfo,
  IngestJob,
  KBDocument,
  KBFolder,
  ModelProfile,
  Session,
  Source,
  TraceStep,
} from "./types";

interface ChatCallbacks {
  onSources: (items: Source[]) => void;
  onToken: (delta: string) => void;
  onTrace: (step: TraceStep) => void;
  onDone: (done: DonePayload) => void;
  onError: (message: string, code?: number) => void;
}

export async function sendChat(
  message: string,
  mode: ChatMode,
  sessionId: string | null,
  cb: ChatCallbacks,
  signal?: AbortSignal,
  profile?: string | null,
  folder?: string | null,
  folders?: string[] | null,
): Promise<void> {
  await postSSE(
    "/api/chat",
    {
      message,
      mode,
      ...(sessionId ? { sessionId } : {}),
      ...(profile ? { profile } : {}),
      ...(folder ? { folder } : {}),
      ...(folders && folders.length > 0 ? { folders } : {}),
    },
    (ev) => {
      const data = ev.data as Record<string, unknown>;
      if (ev.event === "sources") {
        cb.onSources((data.items as Source[]) ?? []);
      } else if (ev.event === "token") {
        cb.onToken(String(data.delta ?? ""));
      } else if (ev.event === "trace") {
        cb.onTrace(data as unknown as TraceStep);
      } else if (ev.event === "done") {
        cb.onDone(data as unknown as DonePayload);
      } else if (ev.event === "error") {
        const err = data as { message?: string; code?: number };
        cb.onError(err.message ?? `错误 ${err.code ?? ""}`, err.code);
      }
    },
    signal,
  );
}

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      message = body?.error?.message ?? message;
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(message);
  }
  return (await resp.json()) as T;
}

export async function fetchHealth(): Promise<HealthInfo> {
  return jsonOrThrow<HealthInfo>(await fetch("/api/health"));
}

export async function fetchDocuments(folder?: string | null): Promise<KBDocument[]> {
  const query = folder ? `?folder=${encodeURIComponent(folder)}` : "";
  const body = await jsonOrThrow<{ data: KBDocument[] }>(
    await fetch(`/api/kb/documents${query}`),
  );
  return body.data ?? [];
}

export async function fetchFolders(): Promise<KBFolder[]> {
  const body = await jsonOrThrow<{ data: KBFolder[] }>(await fetch("/api/kb/folders"));
  return body.data ?? [];
}

export async function createFolder(path: string): Promise<KBFolder> {
  const body = await jsonOrThrow<{ data: KBFolder }>(
    await fetch("/api/kb/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  );
  return body.data;
}

export async function renameFolder(source: string, target: string): Promise<KBFolder> {
  const body = await jsonOrThrow<{ data: KBFolder }>(
    await fetch("/api/kb/folders", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: source, to: target }),
    }),
  );
  return body.data;
}

export async function deleteFolder(path: string, mode: "move" | "delete"): Promise<void> {
  await jsonOrThrow(
    await fetch(
      `/api/kb/folders?path=${encodeURIComponent(path)}&mode=${encodeURIComponent(mode)}`,
      { method: "DELETE" },
    ),
  );
}

export async function moveDocument(docId: string, folder: string): Promise<void> {
  await jsonOrThrow(
    await fetch(`/api/kb/documents/${docId}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }),
    }),
  );
}

export interface PreviewIssue {
  level: "info" | "warning" | "error";
  message: string;
  hint: string;
}

export interface PreviewReport {
  filename: string;
  status: "ok" | "warning" | "rejected";
  pages: number | null;
  estimatedChunks: number;
  tables: number;
  language: string;
  duplicated: boolean;
  issues: PreviewIssue[];
  classification?: ClassificationResult;
}

async function postFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return jsonOrThrow<T>(await fetch(path, { method: "POST", body: form }));
}

export async function previewDocument(
  file: File,
  classify = false,
): Promise<PreviewReport> {
  const suffix = classify ? "?classify=1" : "";
  const body = await postFile<{ data: PreviewReport }>(`/api/kb/preview${suffix}`, file);
  return body.data;
}

export async function classifyDocument(
  docId: string,
  profile?: string | null,
): Promise<ClassificationResult> {
  const body = await jsonOrThrow<{ data: ClassificationResult }>(
    await fetch("/api/kb/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ docId, ...(profile ? { profile } : {}) }),
    }),
  );
  return body.data;
}

export async function sendClassifyFeedback(payload: {
  docId?: string | null;
  suggested?: string | null;
  chosen?: string | null;
  action: "accept" | "adjust" | "reject";
  source?: string;
}): Promise<void> {
  await jsonOrThrow(
    await fetch("/api/kb/classify/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function batchMoveDocuments(
  docIds: string[],
  folder: string,
): Promise<{
  operationId: string | null;
  moved: string[];
  skipped: string[];
  skippedDetails: { docId: string; reason: string }[];
  failed: { docId: string; reason: string }[];
}> {
  const body = await jsonOrThrow<{
    data: {
      operationId: string | null;
      moved: string[];
      skipped: string[];
      skippedDetails: { docId: string; reason: string }[];
      failed: { docId: string; reason: string }[];
    };
  }>(
    await fetch("/api/kb/documents/batch-move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ docIds, folder }),
    }),
  );
  return body.data;
}

export async function undoMove(operationId: string): Promise<void> {
  await jsonOrThrow(
    await fetch(`/api/kb/moves/${operationId}/undo`, { method: "POST" }),
  );
}

export async function startIngestJob(
  file: File,
  folder: string,
): Promise<{ jobId: string; totalDocs: number }> {
  const form = new FormData();
  form.append("file", file);
  form.append("folder", folder);
  const body = await jsonOrThrow<{ data: { jobId: string; totalDocs: number } }>(
    await fetch("/api/kb/ingest/jobs", { method: "POST", body: form }),
  );
  return body.data;
}

export async function fetchIngestJob(jobId: string): Promise<IngestJob> {
  const body = await jsonOrThrow<{ data: IngestJob }>(
    await fetch(`/api/kb/ingest/jobs/${jobId}`),
  );
  return body.data;
}

export async function deleteDocument(docId: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/kb/documents/${docId}`, { method: "DELETE" }));
}

export async function fetchSessions(): Promise<Session[]> {
  const body = await jsonOrThrow<{ data: Session[] }>(await fetch("/api/sessions"));
  return body.data ?? [];
}

export async function fetchModels(): Promise<ModelProfile[]> {
  const body = await jsonOrThrow<{ data: ModelProfile[] }>(await fetch("/api/models"));
  return body.data ?? [];
}

export async function fetchSession(sessionId: string): Promise<Session> {
  const body = await jsonOrThrow<{ data: Session }>(
    await fetch(`/api/sessions/${sessionId}`),
  );
  return body.data;
}

export async function createSession(title?: string): Promise<Session> {
  const body = await jsonOrThrow<{ data: Session }>(
    await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title ?? "" }),
    }),
  );
  return body.data;
}

export async function renameSession(sessionId: string, title: string): Promise<Session> {
  const body = await jsonOrThrow<{ data: Session }>(
    await fetch(`/api/sessions/${sessionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),
  );
  return body.data;
}

export async function deleteSession(sessionId: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/sessions/${sessionId}`, { method: "DELETE" }));
}

export type { ChatMode };
