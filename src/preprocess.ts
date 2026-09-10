import type {
  Evidence, FileReport, Limits, LogPackage, Pattern, Progress, Severity,
} from './types';
import { readSources } from './sources';
import { readLines } from './lines';
import { BalancedSample, type Sample } from './selection';

const emptyCounts = (): Record<Severity, number> => ({
  debug: 0, info: 0, warn: 0, error: 0, fatal: 0, unknown: 0,
});

type Parsed = { severity: Severity; message: string; timestamp?: string; malformedJson?: boolean };

function level(value: unknown): Severity | undefined {
  if (typeof value !== 'string') return undefined;
  const text = value.toLowerCase();
  if (text === 'warning') return 'warn';
  if (text === 'critical' || text === 'crit') return 'fatal';
  return (['debug', 'info', 'warn', 'error', 'fatal'] as const).find((item) => item === text);
}

/** Parse only explicit log-level fields/tokens. Unknown does not mean healthy. */
export function parseLine(line: string): Parsed {
  const trimmed = line.trim();
  if (trimmed.startsWith('{')) {
    try {
      const json: unknown = JSON.parse(trimmed);
      if (json && typeof json === 'object' && !Array.isArray(json)) {
        const record = json as Record<string, unknown>;
        const message = record.message ?? record.msg;
        const timestamp = record.timestamp ?? record.time ?? record.ts;
        return {
          severity: level(record.severity) ?? level(record.level) ?? 'unknown',
          message: typeof message === 'string' ? message : trimmed,
          timestamp: typeof timestamp === 'string' ? timestamp : undefined,
        };
      }
    } catch {
      return { severity: 'unknown', message: trimmed, malformedJson: true };
    }
  }

  let match = /^\[([^\]]+)\]\s+\[(DEBUG|INFO|WARNING|WARN|ERROR|FATAL)\]\s+\[[^\]]+\]\s*(.*)$/i.exec(trimmed);
  if (match) return { timestamp: match[1], severity: level(match[2]) ?? 'unknown', message: match[3] };

  match = /^(\d{4}\/\d{2}\/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\s+\d+)?)\s+(DEBUG|INFO|WARNING|WARN|ERROR|FATAL)\s+\[[^\]]+\]\s*(.*)$/i.exec(trimmed);
  if (match) return { timestamp: match[1], severity: level(match[2]) ?? 'unknown', message: match[3] };

  // Walker exports use a severity letter followed by a full wall-clock timestamp.
  match = /^([DIWEF])(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\[[^\]]+\]\s+\d+\s+\[[^\]]+\]:\s*(.*)$/.exec(trimmed);
  if (match) {
    const levels: Record<string, Severity> = { D: 'debug', I: 'info', W: 'warn', E: 'error', F: 'fatal' };
    return { timestamp: match[2], severity: levels[match[1]], message: match[3] };
  }

  // glog's leading letter is its severity and the timestamp deliberately has no year.
  match = /^([DIWEF])(\d{4}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\d+\s+[^\]]+\]\s*(.*)$/.exec(trimmed);
  if (match) {
    const glog: Record<string, Severity> = { D: 'debug', I: 'info', W: 'warn', E: 'error', F: 'fatal' };
    return { timestamp: match[2], severity: glog[match[1]], message: match[3] };
  }

  match = /^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+[^:]+:\s*(.*)$/.exec(trimmed);
  if (match) return { timestamp: match[1], severity: 'unknown', message: match[2] };

  // The boundaries make this an explicit token, rather than treating prose such as
  // "no error occurred" as an error event.
  match = /^(?:\[(DEBUG|INFO|WARNING|WARN|ERROR|FATAL)\]|(DEBUG|INFO|WARNING|WARN|ERROR|FATAL)(?=\s|:|$))|\blevel\s*[=:]\s*(DEBUG|INFO|WARNING|WARN|ERROR|FATAL)\b/i.exec(trimmed);
  return { severity: level(match?.[1] ?? match?.[2] ?? match?.[3]) ?? 'unknown', message: trimmed };
}

function signature(message: string): string {
  return message.replace(/\s+/g, ' ').trim();
}

const diagnosticKeyword = /\b(?:kernel panic|segfault|out of memory|i\/o error|watchdog|timed out|failed to|thermal throttling)\b/i;
const lifecycleKeyword = /\b(?:start(?:ed|ing)?|initializ(?:e|ed|ing)|config(?:ure|ured|uration)|recover(?:ed|ing|y)?|cleared|online|reset|restart(?:ed|ing)?|ready|shutdown|state|damping|fsm_transition|连接成功)\b/i;

export async function preprocess(
  files: File[], limits: Limits, onProgress?: (p: Progress) => void,
): Promise<LogPackage> {
  const packageFiles: FileReport[] = [];
  type EventSample = Evidence & Sample;
  type PatternSample = Pattern & Sample & { key: string };
  const contextCapacity = limits.maxEvidence > 1 ? Math.max(1, Math.floor(limits.maxEvidence / 4)) : 0;
  const faults = new BalancedSample<EventSample>(limits.maxEvidence - contextCapacity);
  const anchorCapacity = contextCapacity > 1 ? Math.max(1, Math.floor(contextCapacity * .4)) : 0;
  const context = new BalancedSample<EventSample>(contextCapacity - anchorCapacity);
  const anchors = new BalancedSample<EventSample>(anchorCapacity);
  const patternSamples = new BalancedSample<PatternSample>(limits.maxPatterns);
  const patterns = new Map<string, PatternSample>();
  const warnings: string[] = [];
  const totals = { files: 0, lines: 0, expandedBytes: 0, severityCounts: emptyCounts(), evidenceOmitted: 0, patternEventsOmitted: 0 };
  let totalRead = 0;
  let sourceIndex = 0;
  let lastProgress = 0;

  for await (const source of readSources(files, limits)) {
    sourceIndex += 1;
    const path = source.path;
    const memberPath = source.path.split('!/')[1] ?? source.path;
    const memberParts = memberPath.split('/');
    const service = memberParts.find((part) => /^(?:xsys|power(?:_board)?|motor|kernel|control|can|xmigcs)$/i.test(part)) ?? memberParts.at(-2) ?? memberParts.at(-1)?.replace(/\.[^.]+$/, '') ?? 'unknown';
    const node = memberParts.find(part => /^node\d+$/i.test(part));
    const subsystem = node && service !== node ? `${node}/${service}` : service;
    const report: FileReport = { sourceId: source.id, sourceIndex: source.sourceIndex, subsystem, path, sizeBytes: source.sizeBytes, expandedBytes: 0, lines: 0, status: source.status ?? 'processed', reason: source.reason, severityCounts: emptyCounts(), malformedLines: 0, malformedTextLines: 0, truncatedLines: 0, candidateEvents: 0, evidenceRetained: 0, evidenceOmitted: 0, retrieval: source.chunks ? 'rescan-original' : 'unsupported' };
    let before: string[] = [];
    let waiting: EventSample[] = [];
    let lastRoutine: EventSample | undefined;
    const contextLines = Math.max(0, Math.min(20, limits.contextLines ?? 5));
    const metadataFile = /(?:\.(?:ya?ml|ini|conf|cfg|json)|(?:^|\/)(?:metadata|timezone|version))$/i.test(memberPath);

    const retain = (item: EventSample, pool: BalancedSample<EventSample>) => {
      report.candidateEvents!++;
      const displaced = pool.offer(item);
      waiting = waiting.filter(value => value !== displaced);
      if (displaced !== item && contextLines) waiting.push(item);
    };

    const emitProgress = (complete: boolean, force = false) => {
      const now = performance.now();
      if (force || now - lastProgress >= 100) {
        lastProgress = now;
        onProgress?.({ filesCompleted: complete ? sourceIndex : sourceIndex - 1, filesTotal: files.length, currentFile: path, bytesRead: report.expandedBytes, lines: report.lines });
      }
    };

    const handleLine = (line: string, truncated = false, damaged = false) => {
      report.lines += 1; totals.lines += 1;
      const damagedText = damaged || line.includes('\0') || line.includes('\uFFFD');
      // A clipped line contains an explicit omission marker; recognize its prefix,
      // but retain both readable ends, never a fabricated "line omitted" replacement.
      const parsed = parseLine(truncated ? line.split('\n')[0] : line);
      if (truncated) parsed.message = line;
      report.severityCounts[parsed.severity] += 1; totals.severityCounts[parsed.severity] += 1;
      if (parsed.malformedJson) report.malformedLines += 1;
      if (truncated) report.truncatedLines += 1;
      if (damagedText) report.malformedTextLines = (report.malformedTextLines ?? 0) + 1;
      if (parsed.timestamp !== undefined) { report.firstTimestamp ??= parsed.timestamp; report.lastTimestamp = parsed.timestamp; }
      const contextLine = line;
      for (const item of waiting) item.contextAfter.push(contextLine);
      for (let i = waiting.length - 1; i >= 0; i -= 1) if (waiting[i].contextAfter.length >= contextLines) waiting.splice(i, 1);
      const explicitSeverity = parsed.severity === 'warn' || parsed.severity === 'error' || parsed.severity === 'fatal';
      const keywordCandidate = parsed.severity === 'unknown' && diagnosticKeyword.test(parsed.message);
      const lifecycleCandidate = !explicitSeverity && !keywordCandidate && lifecycleKeyword.test(parsed.message);
      // A small deterministic routine stratum keeps time coverage without retaining INFO floods.
      const routineCandidate = report.lines === 1 || report.lines % 1000 === 0;
      const item: EventSample = {
          sourceId: source.id, sourceIndex: source.sourceIndex, subsystem, file: path, line: report.lines, severity: parsed.severity, timestamp: parsed.timestamp,
          message: parsed.message, contextBefore: [...before], contextAfter: [], rawLine: line,
          selectionReason: explicitSeverity ? 'explicit-severity' : keywordCandidate ? 'diagnostic-keyword' : metadataFile ? 'metadata' : lifecycleCandidate ? 'lifecycle' : 'representative',
          textQuality: [...(truncated ? ['clipped-head-tail'] : []), ...(damagedText ? ['damaged-encoding-or-nul'] : [])],
          signature: signature(parsed.message),
          priority: ({ fatal: 6, error: 5, warn: 3, unknown: 1, info: 1, debug: 0 })[parsed.severity] + (lifecycleCandidate ? 2 : 0),
        };
      const selected = explicitSeverity || keywordCandidate || lifecycleCandidate || routineCandidate || (metadataFile && report.lines <= 20);
      if (selected) retain(item, explicitSeverity || keywordCandidate ? faults : lifecycleCandidate && !metadataFile ? context : anchors);
      // The last ordinary line is another time-coverage anchor, selected at EOF.
      lastRoutine = selected ? undefined : item;
      if (explicitSeverity || keywordCandidate) {
        const normalized = signature(parsed.message);
        const key = JSON.stringify([source.sourceIndex, subsystem, parsed.severity, normalized, truncated ? `${source.id}:${report.lines}` : '']);
        const existing = patterns.get(key);
        if (existing) existing.count += 1;
        else {
          const pattern: PatternSample = { sourceIndex: source.sourceIndex, sourceId: source.id, subsystem, signature: normalized, line: report.lines, priority: item.priority, key, severity: parsed.severity, count: 1, countComplete: totals.patternEventsOmitted === 0 && !truncated, example: { file: path, line: report.lines, message: parsed.message } };
          const displaced = patternSamples.offer(pattern);
          if (displaced === pattern) totals.patternEventsOmitted++;
          else {
            if (displaced) { totals.patternEventsOmitted += displaced.count; patterns.delete(displaced.key); }
            patterns.set(key, pattern);
          }
        }
      }
      before = contextLines ? [...before, contextLine].slice(-contextLines) : [];
    };

    try {
      if (!source.chunks) {
        if (report.status === 'processed') { report.status = 'skipped'; report.reason = report.reason ?? 'No readable text stream.'; }
        if (report.reason) warnings.push(`${path}: ${report.reason}`);
      } else {
        for await (const textLine of readLines(source.chunks, limits.maxLineChars, (bytes) => {
          const remaining = Math.min(limits.maxExpandedBytesPerFile - report.expandedBytes, limits.maxTotalExpandedBytes - totalRead);
          if (bytes > remaining) throw new Error('Expanded-byte limit reached.');
          report.expandedBytes += bytes; totalRead += bytes; totals.expandedBytes += bytes;
        })) {
          handleLine(textLine.text, textLine.truncated, textLine.damaged);
          emitProgress(false);
        }
      }
    } catch (error) {
      report.status = report.expandedBytes > 0 || report.lines > 0 ? 'partial' : 'error';
      const detail = error instanceof Error ? error.message : String(error);
      report.reason = detail;
      warnings.push(`${path}: ${report.reason}`);
    }
    if ((report.malformedTextLines ?? 0) > 0 && report.status !== 'error' && report.status !== 'skipped') {
      const damage = `${report.malformedTextLines} damaged text line(s); readable excerpts retained and marked (encoding or NUL).`;
      report.status = 'partial'; report.reason = report.reason ? `${report.reason} ${damage}` : damage;
      warnings.push(`${path}: ${damage}`);
    }
    if (lastRoutine) retain(lastRoutine, anchors);
    if (report.status === 'partial' || report.status === 'error') {
      for (const pattern of patterns.values()) if (pattern.sourceIndex === source.sourceIndex && pattern.subsystem === subsystem) pattern.countComplete = false;
    }
    packageFiles.push(report); totals.files += 1;
    emitProgress(true, true);
  }
  const catalog = files.map((file, index) => ({ id: `source-${index}`, name: file.webkitRelativePath || file.name, sizeBytes: file.size, lastModified: file.lastModified }));
  const evidence: Evidence[] = [...faults.items, ...context.items, ...anchors.items].map(({ priority: _priority, signature: _signature, ...item }) => item).sort((a, b) => a.sourceIndex - b.sourceIndex || a.file.localeCompare(b.file) || a.line - b.line);
  for (const report of packageFiles) {
    report.evidenceRetained = evidence.filter(item => item.sourceId === report.sourceId).length;
    report.evidenceOmitted = Math.max(0, report.candidateEvents! - report.evidenceRetained);
    totals.evidenceOmitted += report.evidenceOmitted;
  }
  const outputPatterns: Pattern[] = [...patterns.values()].map(({ priority: _priority, key: _key, line: _line, sourceId: _sourceId, ...pattern }) => pattern);
  const partialArchives = new Set(packageFiles.filter(file => file.status === 'partial' || file.status === 'error').map(file => file.sourceIndex));
  for (const pattern of outputPatterns) if (partialArchives.has(pattern.sourceIndex)) pattern.countComplete = false;
  return { schemaVersion: 'robot-log-evidence/v2', createdAt: new Date().toISOString(), toolVersion: '0.2.0', analysisStatus: 'preprocessed-only', source: { selectedFiles: files.length, selectedBytes: files.reduce((n, file) => n + file.size, 0), catalog, retrieval: 'browser-session-rescan' }, limits, totals, files: packageFiles, evidence, patterns: outputPatterns, warnings, selection: { strategy: 'bounded archive/node-subsystem/file-balanced sampling; 75% faults, 15% lifecycle, 10% metadata/time anchors; up to 3 exact repeats per file/severity', contextLines: limits.contextLines ?? 5, sourcesWithCandidates: new Set(packageFiles.filter((file) => file.candidateEvents).map((file) => file.sourceIndex)).size, sourcesRepresented: new Set(evidence.map((item) => item.sourceIndex)).size, filesWithCandidates: packageFiles.filter((file) => file.candidateEvents).length, filesRepresented: new Set(evidence.map((item) => item.sourceId)).size } };
}
