import { postSSE } from "./sse";
import type {
  AttachmentInfo,
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
  UploadChunkResult,
  UploadInit,
  UploadStatus,
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
  attachmentIds?: string[] | null,
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
      ...(attachmentIds && attachmentIds.length > 0 ? { attachmentIds } : {}),
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

type CodedError = Error & { code?: number };

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`;
    let code: number | undefined;
    try {
      const body = await resp.json();
      message = body?.error?.message ?? message;
      code = body?.error?.code;
    } catch {
      /* 非 JSON 错误体 */
    }
    const error = new Error(message) as CodedError;
    error.code = code;
    throw error;
  }
  return (await resp.json()) as T;
}

function chunkError(xhr: XMLHttpRequest): CodedError {
  let message = `HTTP ${xhr.status}`;
  let code: number | undefined;
  try {
    const body = JSON.parse(xhr.responseText);
    message = body?.error?.message ?? message;
    code = body?.error?.code;
  } catch {
    /* 非 JSON 错误体 */
  }
  const error = new Error(message) as CodedError;
  error.code = code;
  return error;
}

export async function initUpload(payload: {
  sessionId?: string | null;
  filename: string;
  size: number;
  mime?: string;
}): Promise<UploadInit> {
  const body = await jsonOrThrow<{ data: UploadInit }>(
    await fetch("/api/uploads/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(payload.sessionId ? { sessionId: payload.sessionId } : {}),
        filename: payload.filename,
        size: payload.size,
        ...(payload.mime ? { mime: payload.mime } : {}),
      }),
    }),
  );
  return body.data;
}

export function uploadChunk(
  uploadId: string,
  index: number,
  chunk: Blob,
  sessionId: string,
  onProgress: (ratio: number) => void,
  signal?: AbortSignal,
): Promise<UploadChunkResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;
    const cleanup = () => {
      signal?.removeEventListener("abort", onAbort);
    };
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn();
    };
    const onAbort = () => xhr.abort();
    xhr.open(
      "PUT",
      `/api/uploads/${encodeURIComponent(uploadId)}/chunks/${index}?sessionId=${encodeURIComponent(sessionId)}`,
    );
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 120_000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const body = JSON.parse(xhr.responseText) as { data: UploadChunkResult };
          finish(() => resolve(body.data));
        } catch {
          finish(() => reject(new Error("上传响应解析失败")));
        }
      } else {
        finish(() => reject(chunkError(xhr)));
      }
    };
    xhr.onerror = () => finish(() => reject(new Error("网络中断，上传已暂停")));
    xhr.ontimeout = () => finish(() => reject(new Error("分片上传超时")));
    xhr.onabort = () => {
      const error = new Error("上传已取消") as CodedError;
      error.name = "AbortError";
      finish(() => reject(error));
    };
    if (signal) {
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener("abort", onAbort, { once: true });
    }
    xhr.send(chunk);
  });
}

export async function fetchUploadStatus(
  uploadId: string,
  sessionId: string,
): Promise<UploadStatus> {
  const body = await jsonOrThrow<{ data: UploadStatus }>(
    await fetch(
      `/api/uploads/${encodeURIComponent(uploadId)}?sessionId=${encodeURIComponent(sessionId)}`,
    ),
  );
  return body.data;
}

export async function completeUpload(
  uploadId: string,
  sessionId: string,
): Promise<AttachmentInfo> {
  const body = await jsonOrThrow<{ data: AttachmentInfo }>(
    await fetch(`/api/uploads/${encodeURIComponent(uploadId)}/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId }),
    }),
  );
  return body.data;
}

export async function abortUpload(uploadId: string, sessionId: string): Promise<void> {
  await jsonOrThrow(
    await fetch(
      `/api/uploads/${encodeURIComponent(uploadId)}?sessionId=${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function deleteAttachment(
  attachmentId: string,
  sessionId: string,
): Promise<void> {
  await jsonOrThrow(
    await fetch(
      `/api/attachments/${encodeURIComponent(attachmentId)}?sessionId=${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function listSessionAttachments(
  sessionId: string,
): Promise<AttachmentInfo[]> {
  const body = await jsonOrThrow<{ data: AttachmentInfo[] }>(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`),
  );
  return body.data ?? [];
}

export function attachmentUrl(
  attachmentId: string,
  sessionId: string,
  download = false,
): string {
  const url = `/api/attachments/${encodeURIComponent(attachmentId)}/content?sessionId=${encodeURIComponent(sessionId)}`;
  return download ? `${url}&download=1` : url;
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
