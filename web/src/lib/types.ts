export interface Source {
  index: number;
  docId: string;
  docName: string;
  section: string;
  page: number | null;
  locator: string | null;
  score: number | null;
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
  chunks: number;
  ingestedAt: string | null;
}

export type ChatMode = "kb" | "direct";
