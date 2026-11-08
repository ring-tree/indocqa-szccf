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

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  trace?: TraceStep[];
  confidence?: string | null;
  confidenceReason?: string;
  streaming?: boolean;
}

export interface KBDocument {
  docId: string;
  filename: string;
  chunks: number;
  ingestedAt: string | null;
}

export type ChatMode = "kb" | "direct";
