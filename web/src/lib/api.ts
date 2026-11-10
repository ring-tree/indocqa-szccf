import { postSSE } from "./sse";
import type {
  ChatMode,
  DonePayload,
  KBDocument,
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
  onError: (message: string) => void;
}

export async function sendChat(
  message: string,
  mode: ChatMode,
  sessionId: string | null,
  cb: ChatCallbacks,
  signal?: AbortSignal,
  profile?: string | null,
): Promise<void> {
  await postSSE(
    "/api/chat",
    {
      message,
      mode,
      ...(sessionId ? { sessionId } : {}),
      ...(profile ? { profile } : {}),
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
        cb.onError(err.message ?? `错误 ${err.code ?? ""}`);
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

export async function fetchDocuments(): Promise<KBDocument[]> {
  const body = await jsonOrThrow<{ data: KBDocument[] }>(await fetch("/api/kb/documents"));
  return body.data ?? [];
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
}

async function postFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return jsonOrThrow<T>(await fetch(path, { method: "POST", body: form }));
}

export async function previewDocument(file: File): Promise<PreviewReport> {
  const body = await postFile<{ data: PreviewReport }>("/api/kb/preview", file);
  return body.data;
}

export async function uploadDocument(
  file: File,
): Promise<{ filename: string; chunks: number; duplicated: boolean }> {
  const body = await postFile<{ data: { filename: string; chunks: number; duplicated: boolean } }>(
    "/api/kb/ingest",
    file,
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
