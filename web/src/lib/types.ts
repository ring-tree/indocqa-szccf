export interface Source {
  index: number;
  docId: string;
  docName: string;
  section: string;
  page: number | null;
  locator: string | null;
  score: number | null;
  folder?: string;
  snippet?: string;
}

export interface TraceStep {
  stage: string;
  intent?: string;
  plan?: string;
  hop?: number;
  strategy?: string;
  query?: string;
  expansions?: string[];
  hits?: number;
  folder?: string;
  result?: string;
}

export interface DonePayload {
  confidence: string | null;
  confidence_reason: string;
  sessionId?: string | null;
  messageId?: number | null;
  boundary?: string | null;
  searched_scope?: string[];
  model?: string | null;
}

export interface ModelProfile {
  id: string;
  model: string;
}

export interface ChatMessage {
  id?: number;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  trace?: TraceStep[];
  confidence?: string | null;
  confidenceReason?: string;
  boundary?: string | null;
  model?: string | null;
  streaming?: boolean;
  stopped?: boolean;
  error?: boolean;
}

export interface Session {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount?: number;
  lastMessage?: string;
  messages?: ChatMessage[];
}

export interface KBDocument {
  docId: string;
  filename: string;
  folder?: string;
  chunks: number;
  pages?: number | null;
  tables?: number | null;
  ingestedAt: string | null;
}

export interface KBFolder {
  path: string;
  name: string;
  parent: string;
  documentCount: number;
  chunkCount: number;
}

export interface ClassificationCandidate {
  path: string;
  confidence: number;
  level: "high" | "mid" | "low";
  reason: string;
  evidence: string[];
}

export interface ClassificationResult {
  available: boolean;
  candidates?: ClassificationCandidate[];
  suggested_new_folder?: {
    path: string;
    name: string;
    reason: string;
    confidence: number;
    isNew?: boolean;
  } | null;
  fallback?: boolean;
  reason?: string;
  latencyMs?: number;
  model?: string | null;
  error?: string;
}

export interface EmbeddingStatus {
  mode: string;
  backend: string;
  model: string;
}

export interface HealthInfo {
  status: string;
  version: string;
  kb: { documents: number | null; chunks: number | null; state: string };
  llm: { configured: boolean; reachable: boolean | null };
  embedding: EmbeddingStatus;
}

export interface IngestJob {
  jobId: string;
  status: "queued" | "running" | "done" | "error";
  percent: number;
  currentDoc: number;
  totalDocs: number;
  message: string;
  result?: { filename: string; chunks: number; duplicated: boolean; folder: string };
  error?: string | null;
}

export type ChatMode = "kb" | "direct";
