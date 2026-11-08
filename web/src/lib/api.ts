import { postSSE } from "./sse";
import type { ChatMode, KBDocument, Source, TraceStep } from "./types";

interface ChatCallbacks {
  onSources: (items: Source[]) => void;
  onToken: (delta: string) => void;
  onTrace: (step: TraceStep) => void;
  onDone: (confidence: string | null, reason: string) => void;
  onError: (message: string) => void;
}

export async function sendChat(message: string, mode: ChatMode, cb: ChatCallbacks): Promise<void> {
  await postSSE("/api/chat", { message, mode }, (ev) => {
    const data = ev.data as Record<string, unknown>;
    if (ev.event === "sources") {
      cb.onSources((data.items as Source[]) ?? []);
    } else if (ev.event === "token") {
      cb.onToken(String(data.delta ?? ""));
    } else if (ev.event === "trace") {
      cb.onTrace(data as unknown as TraceStep);
    } else if (ev.event === "done") {
      cb.onDone(
        (data.confidence as string | null) ?? null,
        String(data.confidence_reason ?? ""),
      );
    } else if (ev.event === "error") {
      const err = data as { message?: string; code?: number };
      cb.onError(err.message ?? `错误 ${err.code ?? ""}`);
    }
  });
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

export async function uploadDocument(file: File): Promise<{ filename: string; chunks: number; duplicated: boolean }> {
  const body = await postFile<{ data: { filename: string; chunks: number; duplicated: boolean } }>(
    "/api/kb/ingest",
    file,
  );
  return body.data;
}

export async function deleteDocument(docId: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/kb/documents/${docId}`, { method: "DELETE" }));
}

export type { ChatMode };