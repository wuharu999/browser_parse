import { describe, expect, it } from 'vitest';
import { buildBrief } from '../src/brief';
import type { LogPackage } from '../src/types';

const fixture = (): LogPackage => ({ schemaVersion: 'robot-log-evidence/v2', toolVersion: '0.2.0', createdAt: '2026', analysisStatus: 'preprocessed-only', limits: {} as LogPackage['limits'], source: { selectedFiles: 2, selectedBytes: 1, retrieval: 'browser-session-rescan', catalog: [{ id: 'source-0', name: `甲${'a'.repeat(1000)}`, sizeBytes: 1, lastModified: 0 }, { id: 'source-1', name: `乙${'b'.repeat(1000)}`, sizeBytes: 1, lastModified: 0 }] }, totals: { files: 2, lines: 2, expandedBytes: 1, severityCounts: { debug: 0, info: 0, warn: 1, error: 1, fatal: 0, unknown: 0 }, evidenceOmitted: Number.NaN, patternEventsOmitted: Infinity }, files: [], patterns: [], warnings: [], selection: { strategy: 'test', contextLines: 5, sourcesWithCandidates: 2, sourcesRepresented: 2, filesWithCandidates: 2, filesRepresented: 2 }, evidence: [0, 1].map((sourceIndex) => ({ sourceId: `source-${sourceIndex}`, sourceIndex, subsystem: 'motor', file: `a${sourceIndex}`, line: 1, severity: sourceIndex ? 'error' : 'warn', message: `head-${sourceIndex}-${'中'.repeat(500)}-tail-${sourceIndex}`, contextBefore: [], contextAfter: [] })) as LogPackage['evidence'] });

describe('brief byte budget', () => {
  it('strictly serializes within the minimum UTF-8 budget and keeps archive diversity', () => {
    const result = buildBrief(fixture(), 2048);
    expect(result.bytes).toBeLessThanOrEqual(2048);
    expect(new TextEncoder().encode(result.json).byteLength).toBe(result.bytes);
    expect(result.json).not.toContain('NaN');
  });

  it('clips messages with both head and tail preserved', () => {
    const result = buildBrief(fixture(), 4096);
    expect(result.brief.evidence.map(item => item.sourceId)).toEqual(['source-0', 'source-1']);
    expect(result.brief.evidence[0].message).toContain('head-0');
    expect(result.brief.evidence[0].message).toContain('tail-0');
  });
});
