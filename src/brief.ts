import type { Evidence, LogPackage } from './types';

export const BRIEF_MAX_BYTES = 16000;
const encoder = new TextEncoder();
const encodedSize = (value: unknown) => encoder.encode(JSON.stringify(value)).byteLength;
const finite = (value: number) => Number.isFinite(value) ? value : 0;
function clip(value: string, cap: number) {
  if (cap <= 0) return '';
  if (value.length <= cap) return value;
  const head = Math.ceil(cap * .7); const tail = Math.max(0, cap - head - 28);
  return `${value.slice(0, head)}\n[… ${value.length - head - tail} chars omitted …]\n${tail ? value.slice(-tail) : ''}`;
}
const priority = (event: Evidence) => ({ fatal: 0, error: 1, warn: 2, unknown: 4, info: 5, debug: 6 })[event.severity];

/** A model-independent byte budget: no tokenizer, LLM, or network call is needed. */
export function buildBrief(report: LogPackage, requestedBytes = BRIEF_MAX_BYTES) {
  const maxBytes = Math.max(2048, Math.min(65536, finite(Math.floor(requestedBytes)) || BRIEF_MAX_BYTES));
  const brief = {
    schemaVersion: 'robot-log-brief/v1',
    evidenceSchema: report.schemaVersion,
    createdAt: report.createdAt,
    analysisStatus: report.analysisStatus,
    coverage: {
      sourceFiles: finite(report.source.selectedFiles),
      fileReports: report.files.length,
      linesRead: finite(report.totals.lines),
      severityCounts: report.totals.severityCounts,
      fileStatusCounts: report.files.reduce<Record<string, number>>((counts, file) => {
        counts[file.status] = (counts[file.status] ?? 0) + 1;
        return counts;
      }, {}),
      selection: report.selection,
    },
    omissions: {
      evidenceNotRetained: finite(report.totals.evidenceOmitted),
      patternEventsNotTracked: report.totals.patternEventsOmitted,
      evidenceNotInBrief: report.evidence.length,
      sourcesNotInBrief: report.source.selectedFiles,
    },
    sourceFiles: [] as { id: string; name: string; nameClipped?: boolean }[],
    evidence: [] as {
      sourceId: string; subsystem: string; line: number; severity: string; selectedFor: string;
      timestamp?: string; message: string; excerptClipped?: boolean; quality?: string[];
    }[],
    retrieval: {
      mode: 'browser-session-rescan',
      query: { sourceId: 'source-0/entry-1', startLine: 1, maxLines: 20, maxBytes: 12000 },
      note: 'Original files remain local. Use the file index/context reader in this browser session; a remote agent cannot retrieve them from this JSON alone.',
    },
    interpretation: 'Sampled evidence, not diagnosis. Check file coverage and retrieve wider context/recovery before inferring causes. Damaged or clipped text is marked; raw originals are unchanged.',
  };
  // Spend at most a quarter of the budget on source names; full manifest remains local.
  for (const source of report.source.catalog) {
    const name = clip(source.name, 180);
    const item = { id: source.id, name, ...(source.name.length > name.length ? { nameClipped: true } : {}) };
    if (encodedSize(brief.sourceFiles) + encodedSize(item) > maxBytes / 4) break;
    brief.sourceFiles.push(item);
  }
  brief.omissions.sourcesNotInBrief -= brief.sourceFiles.length;

  // Each round visits every selected archive, then a different subsystem within it.
  const archives = new Map<number, Map<string, Evidence[]>>();
  for (const event of report.evidence) {
    let subsystems = archives.get(event.sourceIndex);
    if (!subsystems) { subsystems = new Map(); archives.set(event.sourceIndex, subsystems); }
    const events = subsystems.get(event.subsystem) ?? [];
    events.push(event);
    subsystems.set(event.subsystem, events);
  }
  const queues: Evidence[][] = [];
  for (const [, subsystems] of [...archives].sort(([a], [b]) => a - b)) {
    const groups = [...subsystems].sort(([a], [b]) => a.localeCompare(b)).map(([, values]) => values.sort((a, b) => priority(a) - priority(b) || a.line - b.line));
    // Visit high-severity subsystems first without exhausting one before visiting others.
    groups.sort((a, b) => priority(a[0]) - priority(b[0]));
    const queue: Evidence[] = [];
    while (groups.some(group => group.length)) for (const group of groups) { const event = group.shift(); if (event) queue.push(event); }
    queues.push(queue);
  }
  while (queues.some(queue => queue.length)) {
    for (const queue of queues) {
      const event = queue.shift();
      if (!event) continue;
      const message = clip(event.message, 400);
      brief.evidence.push({
        sourceId: event.sourceId, subsystem: clip(event.subsystem, 100), line: event.line, severity: event.severity,
        selectedFor: event.selectionReason ?? 'representative', timestamp: event.timestamp,
        message, ...(event.message.length > message.length ? { excerptClipped: true } : {}),
        ...(event.textQuality?.length ? { quality: event.textQuality } : {}),
      });
      brief.omissions.evidenceNotInBrief--;
      if (encodedSize(brief) > maxBytes) {
        brief.evidence.pop(); brief.omissions.evidenceNotInBrief++;
      }
    }
  }
  // Final serialized check: every field is measured after JSON escaping and UTF-8 encoding.
  let json = JSON.stringify(brief);
  while (encoder.encode(json).byteLength > maxBytes && brief.evidence.some((item) => item.message.length > 96)) {
    for (const item of brief.evidence) if (item.message.length > 96) { item.message = clip(item.message, Math.max(96, item.message.length - 80)); item.excerptClipped = true; }
    json = JSON.stringify(brief);
  }
  while (encoder.encode(json).byteLength > maxBytes && brief.evidence.length) { brief.evidence.pop(); brief.omissions.evidenceNotInBrief++; json = JSON.stringify(brief); }
  while (encoder.encode(json).byteLength > maxBytes && brief.sourceFiles.length) { brief.sourceFiles.pop(); brief.omissions.sourcesNotInBrief++; json = JSON.stringify(brief); }
  while (encoder.encode(json).byteLength > maxBytes && brief.interpretation.length) { brief.interpretation = clip(brief.interpretation, Math.max(0, brief.interpretation.length - 128)); json = JSON.stringify(brief); }
  return { brief, json, bytes: encoder.encode(json).byteLength, maxBytes };
}
