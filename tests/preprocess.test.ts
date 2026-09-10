import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_LIMITS, type Limits } from '../src/types';

const state = vi.hoisted(() => ({ sources: [] as Array<{ id?: string; sourceIndex?: number; sourcePath?: string; path: string; sizeBytes: number; chunks?: AsyncIterable<Uint8Array> }> }));
vi.mock('../src/sources', () => ({
  readSources: async function* () { for (const [index, source] of state.sources.entries()) yield { id: `source-${index}`, sourceIndex: index, ...source }; },
}));
const { parseLine, preprocess } = await import('../src/preprocess');

const encoder = new TextEncoder();
const chunks = (...values: string[]): AsyncIterable<Uint8Array> => ({
  async *[Symbol.asyncIterator]() { for (const value of values) yield encoder.encode(value); },
});
const limits = (overrides: Partial<Limits> = {}): Limits => ({ ...DEFAULT_LIMITS, ...overrides });
const input = () => [new Blob(['synthetic'], { type: 'text/plain' }) as File];

describe('robot log recognition', () => {
  it('recognizes Walker severity-letter timestamps without treating elapsed time as wall time', () => {
    expect(parseLine('E2026-03-23 20:22:51.691910 [ 48.514102] 66 [motor.cpp:135:tick]: motor unavailable'))
      .toMatchObject({ severity: 'error', timestamp: '2026-03-23 20:22:51.691910', message: 'motor unavailable' });
  });

  it('preserves raw timestamp spellings across supported formats', () => {
    expect(parseLine('[2026-09-09 13:23:03.984211] [WARNING] [sbus_main.cpp:137] synthetic').timestamp).toBe('2026-09-09 13:23:03.984211');
    expect(parseLine('2026/09/02 17:50:21 209310\tINFO [debugger.cc->onInit:67]\tsynthetic')).toMatchObject({ severity: 'info', timestamp: '2026/09/02 17:50:21 209310' });
    expect(parseLine('W0902 17:50:20.695700 5305 proc_manager.cc:1262] synthetic')).toMatchObject({ severity: 'warn', timestamp: '0902 17:50:20.695700' });
    expect(parseLine('Sep  2 17:50:00 ubuntu kernel: [13.4] synthetic')).toMatchObject({ severity: 'unknown', timestamp: 'Sep  2 17:50:00' });
  });

  it('only classifies explicit generic severity tokens and JSONL fields', () => {
    expect(parseLine('the word error appears in ordinary prose').severity).toBe('unknown');
    expect(parseLine('level=ERROR explicit synthetic event').severity).toBe('error');
    expect(parseLine('{"level":"WARN","msg":"synthetic"}')).toMatchObject({ severity: 'warn', message: 'synthetic' });
    expect(parseLine('{broken').malformedJson).toBe(true);
  });
});

describe('stream preprocessing', () => {
  beforeEach(() => { state.sources = []; });

  it('decodes a UTF-8 character split across chunks and captures two-line context', async () => {
    const bytes = encoder.encode('before one\nbefore two\nERROR caf\u00e9\nafter one\nafter two\n');
    state.sources = [{ path: 'synthetic.log', sizeBytes: bytes.length, chunks: { async *[Symbol.asyncIterator]() { yield bytes.subarray(0, 31); yield bytes.subarray(31); } } }];
    const result = await preprocess(input(), limits());
    expect(result.evidence.find(item => item.severity === 'error')).toMatchObject({ line: 3, message: 'ERROR café', contextBefore: ['before one', 'before two'], contextAfter: ['after one', 'after two'] });
    expect(result.evidence.some(item => item.line === 1 && item.selectionReason === 'representative')).toBe(true);
    expect(result.evidence.some(item => item.line === 5 && item.selectionReason === 'representative')).toBe(true);
  });

  it('marks long-line clipping and reports bounded evidence and pattern omissions', async () => {
    state.sources = [{ path: 'synthetic.log', sizeBytes: 100, chunks: chunks('123456789012345\nERROR first\nERROR second\nWARN third\nFATAL fourth\n') }];
    const result = await preprocess(input(), limits({ maxLineChars: 12, maxEvidence: 1, maxPatterns: 2 }));
    expect(result.files[0].truncatedLines).toBe(1);
    expect(result.files[0].lines).toBe(5);
    expect(result.evidence).toHaveLength(1);
    expect(result.totals.evidenceOmitted).toBe(4);
    expect(result.patterns.length).toBe(2);
    expect(result.totals.patternEventsOmitted).toBeGreaterThan(0);
  });

  it('retains diagnostic-keyword syslog evidence as unknown without diagnosing severity', async () => {
    state.sources = [{ path: 'messages', sizeBytes: 80, chunks: chunks('Sep  2 17:50:00 ubuntu kernel: watchdog timed out on synthetic worker\n') }];
    const result = await preprocess(input(), limits());
    expect(result.evidence[0]).toMatchObject({ severity: 'unknown', selectionReason: 'diagnostic-keyword', rawLine: 'Sep  2 17:50:00 ubuntu kernel: watchdog timed out on synthetic worker' });
  });

  it('replaces retained errors with fatal evidence when the evidence cap is full', async () => {
    state.sources = [{ path: 'synthetic.log', sizeBytes: 40, chunks: chunks('ERROR recoverable synthetic\nFATAL terminal synthetic\n') }];
    const result = await preprocess(input(), limits({ maxEvidence: 1 }));
    expect(result.evidence).toHaveLength(1);
    expect(result.evidence[0].severity).toBe('fatal');
  });

  it('keeps valid neighbours around NUL-padded and invalid UTF-8 lines', async () => {
    const invalid = new Uint8Array([...encoder.encode('INFO before\nERROR important\n\0\0\n'), 0xff, 0x0a, ...encoder.encode('WARN after\n')]);
    state.sources = [{ path: 'padded.log', sizeBytes: invalid.byteLength, chunks: { async *[Symbol.asyncIterator]() { yield invalid; } } }];
    const result = await preprocess(input(), limits());
    expect(result.files[0]).toMatchObject({ status: 'partial', lines: 5, malformedTextLines: 2 });
    expect(result.files[0].reason).toContain('2 damaged text line(s)');
    expect(result.evidence.filter(item => item.selectionReason === 'explicit-severity').map((item) => item.message)).toEqual(['ERROR important', 'WARN after']);
  });

  it('keeps balanced source coverage regardless of archive encounter order', async () => {
    const make = (index: number) => ({ id: `source-${index}`, sourceIndex: index, sourcePath: `archive-${index}.tar`, path: `archive-${index}.tar!/power/node${index}.log`, sizeBytes: 1000, chunks: chunks(Array.from({ length: 20 }, (_, n) => `WARN node ${index} event ${n}\n`).join('')) });
    state.sources = [make(3), make(2), make(1), make(0)];
    const result = await preprocess(input(), limits({ maxEvidence: 20 }));
    expect(result.evidence).toHaveLength(15); // 75% fault stratum, 25% reserved context
    expect(new Set(result.evidence.map((item) => item.sourceId))).toEqual(new Set(['source-0', 'source-1', 'source-2', 'source-3']));
    expect(result.selection.sourcesRepresented).toBe(4);
    expect(result.totals.evidenceOmitted).toBeGreaterThan(0);
  });

  it('preserves a late rare error and lifecycle recovery after warning noise', async () => {
    state.sources = [{ id: 'source-0', sourceIndex: 0, sourcePath: 'a.tar', path: 'a.tar!/control/motor.log', sizeBytes: 5000, chunks: chunks(`${Array.from({ length: 30 }, () => 'WARN repeated noisy warning\n').join('')}ERROR motor 33 code 16\n${Array.from({ length: 10 }, () => 'INFO ordinary\n').join('')}INFO recovery completed motor 33\n`) }];
    const result = await preprocess(input(), limits({ maxEvidence: 20 }));
    expect(result.evidence.some((item) => item.message.includes('motor 33 code 16'))).toBe(true);
    expect(result.evidence.some((item) => item.selectionReason === 'lifecycle' && item.message.includes('recovery completed'))).toBe(true);
  });

  it('keeps numeric motor and code signatures distinct and scoped', async () => {
    state.sources = [{ id: 'source-0', sourceIndex: 0, sourcePath: 'a.tar', path: 'a.tar!/motor/control.log', sizeBytes: 100, chunks: chunks('ERROR motor 33 code 16\nERROR motor 34 code 16\nERROR motor 33 code 32\n') }];
    const result = await preprocess(input(), limits());
    expect(result.patterns.map((pattern) => pattern.signature).sort()).toEqual(['ERROR motor 33 code 16', 'ERROR motor 33 code 32', 'ERROR motor 34 code 16']);
    expect(result.patterns.every((pattern) => pattern.sourceIndex === 0 && pattern.subsystem === 'motor' && pattern.countComplete)).toBe(true);
  });

  it('preserves readable damaged faults and both ends of oversized lines', async () => {
    state.sources = [{ path: 'motor.log', sizeBytes: 300, chunks: chunks(`ERROR motor 33 ${'x'.repeat(100)} code=16\nERROR motor 34\0 code=32\n`) }];
    const result = await preprocess(input(), limits({ maxLineChars: 60 }));
    const long = result.evidence.find(item => item.line === 1)!;
    expect(long.severity).toBe('error');
    expect(long.rawLine).toContain('code=16');
    expect(long.textQuality).toContain('clipped-head-tail');
    const damaged = result.evidence.find(item => item.line === 2)!;
    expect(damaged.rawLine).toContain('motor 34\\0 code=32');
    expect(damaged.textQuality).toContain('damaged-encoding-or-nul');
  });

  it('does not merge numeric identifiers after character 512', async () => {
    const prefix = `ERROR ${'x'.repeat(600)}`;
    state.sources = [{ path: 'motor.log', sizeBytes: 2000, chunks: chunks(`${prefix} motor=33\n${prefix} motor=34\n`) }];
    const result = await preprocess(input(), limits());
    expect(result.patterns).toHaveLength(2);
  });

  it('preserves metadata and normal anchors beside a lifecycle flood, with reconciled omissions', async () => {
    state.sources = [
      { path: 'a.tar!/control/flood.log', sourceIndex: 0, id: 'source-0/entry-0', sizeBytes: 10000, chunks: chunks(Array.from({ length: 100 }, (_, n) => `INFO state ready ${n}\nWARN fault ${n}\n`).join('')) },
      { path: 'a.tar!/config/robot.yaml', sourceIndex: 0, id: 'source-0/entry-1', sizeBytes: 100, chunks: chunks('robot_model: example\ntimezone: Asia/Shanghai\n') },
      { path: 'b.tar!/other/healthy.log', sourceIndex: 1, id: 'source-1/entry-0', sizeBytes: 50, chunks: chunks('INFO routine heartbeat\nINFO routine sample\n') },
    ];
    const result = await preprocess(input(), limits({ maxEvidence: 40 }));
    expect(result.evidence.some(item => item.selectionReason === 'metadata')).toBe(true);
    expect(result.evidence.some(item => item.selectionReason === 'representative' && item.sourceIndex === 1)).toBe(true);
    for (const file of result.files) {
      expect(file.evidenceRetained).toBe(result.evidence.filter(item => item.sourceId === file.sourceId).length);
      expect(file.evidenceOmitted! + file.evidenceRetained!).toBe(file.candidateEvents);
    }
    expect(result.totals.evidenceOmitted).toBe(result.files.reduce((n, file) => n + file.evidenceOmitted!, 0));
    expect(result.patterns.reduce((n, pattern) => n + pattern.count, 0) + result.totals.patternEventsOmitted).toBe(100);
  });

  it('does not claim complete pattern counts after a source read fails', async () => {
    state.sources = [{ path: 'partial.log', sizeBytes: 100, chunks: { async *[Symbol.asyncIterator]() { yield encoder.encode('WARN fault\n'); throw new Error('truncated stream'); } } }];
    const result = await preprocess(input(), limits());
    expect(result.patterns[0].countComplete).toBe(false);
    expect(result.files[0].status).toBe('partial');
  });
});
