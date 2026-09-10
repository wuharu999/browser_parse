export type Severity = 'debug' | 'info' | 'warn' | 'error' | 'fatal' | 'unknown';

export interface Limits {
  maxFiles: number;
  maxLineChars: number;
  maxEvidence: number;
  maxPatterns: number;
  maxExpandedBytesPerFile: number;
  maxTotalExpandedBytes: number;
  contextLines?: number;
}

export const DEFAULT_LIMITS: Limits = {
  maxFiles: 10000,
  maxLineChars: 16384,
  maxEvidence: 500,
  maxPatterns: 200,
  maxExpandedBytesPerFile: 2 * 1024 ** 3,
  maxTotalExpandedBytes: 8 * 1024 ** 3,
  contextLines: 5,
};

export interface Evidence {
  sourceId: string;
  sourceIndex: number;
  subsystem: string;
  file: string;
  line: number;
  severity: Severity;
  timestamp?: string;
  message: string;
  contextBefore: string[];
  contextAfter: string[];
  /** Why this line was retained; unknown severity is never upgraded to an error. */
  selectionReason?: 'explicit-severity' | 'diagnostic-keyword' | 'lifecycle' | 'representative' | 'metadata';
  /** Selected line, with explicit head/tail clipping and encoding markers when necessary. */
  rawLine?: string;
  textQuality?: string[];
  /** Stable group key without erasing motor/channel identifiers or numeric values. */
  signature?: string;
}

export interface FileReport {
  sourceId: string;
  sourceIndex: number;
  subsystem: string;
  path: string;
  sizeBytes: number;
  expandedBytes: number;
  lines: number;
  status: 'processed' | 'partial' | 'skipped' | 'error';
  reason?: string;
  severityCounts: Record<Severity, number>;
  malformedLines: number;
  /** Lines containing NUL padding or invalid UTF-8; readable excerpts are preserved. */
  malformedTextLines?: number;
  truncatedLines: number;
  firstTimestamp?: string;
  lastTimestamp?: string;
  candidateEvents?: number;
  evidenceRetained?: number;
  evidenceOmitted?: number;
  retrieval?: 'rescan-original' | 'unsupported';
}

export interface Pattern {
  sourceIndex: number;
  subsystem: string;
  signature: string;
  severity: Severity;
  count: number;
  countComplete: boolean;
  example: { file: string; line: number; message: string };
}

export interface LogPackage {
  schemaVersion: 'robot-log-evidence/v2';
  createdAt: string;
  toolVersion: '0.2.0';
  analysisStatus: 'preprocessed-only';
  source: {
    selectedFiles: number;
    selectedBytes: number;
    catalog: { id: string; name: string; sizeBytes: number; lastModified: number }[];
    retrieval: 'browser-session-rescan';
  };
  limits: Limits;
  totals: {
    files: number;
    lines: number;
    expandedBytes: number;
    severityCounts: Record<Severity, number>;
    evidenceOmitted: number;
    patternEventsOmitted: number;
  };
  files: FileReport[];
  evidence: Evidence[];
  patterns: Pattern[];
  warnings: string[];
  selection: {
    strategy: string;
    contextLines: number;
    sourcesWithCandidates: number;
    sourcesRepresented: number;
    filesWithCandidates: number;
    filesRepresented: number;
  };
}

export interface ContextQuery {
  sourceId: string;
  startLine: number;
  maxLines: number;
  maxBytes: number;
}

export interface ContextResult {
  sourceId: string;
  path: string;
  startLine: number;
  lines: { line: number; text: string; originalChars: number; truncated: boolean; damaged: boolean }[];
  nextLine?: number;
  truncated: boolean;
  warning?: string;
  integrity: 'not-revalidated';
}

export interface Progress {
  filesCompleted: number;
  filesTotal: number;
  currentFile: string;
  bytesRead: number;
  lines: number;
}

export type WorkerRequest =
  | { type: 'start'; files: File[]; limits: Limits }
  | { type: 'context'; requestId: number; files: File[]; limits: Limits; query: ContextQuery };
export type WorkerResponse =
  | { type: 'progress'; progress: Progress }
  | { type: 'complete'; result: LogPackage }
  | { type: 'context'; requestId: number; result: ContextResult }
  | { type: 'error'; message: string; requestId?: number };
