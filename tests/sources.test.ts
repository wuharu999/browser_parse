import { describe, expect, it } from 'vitest';
import { pack } from 'it-tar';
import { BlobWriter, TextReader, ZipWriter } from '@zip.js/zip.js';
import { preprocess } from '../src/preprocess';
import { readSources } from '../src/sources';
import { DEFAULT_LIMITS } from '../src/types';

async function tarFile(entries: { name: string; text: string; type?: 'file' | 'symlink' }[], gzip = true) {
  const parts: Uint8Array[] = [];
  for await (const part of pack()(entries.map(entry => ({ header: { name: entry.name, type: entry.type ?? 'file' }, body: entry.text })))) parts.push(part);
  const blob = new Blob(parts as BlobPart[]);
  return new File([gzip ? await new Response(blob.stream().pipeThrough(new CompressionStream('gzip'))).blob() : blob], gzip ? 'robot.tar.gz' : 'robot.tar');
}

describe('archive integration', () => {
  it('assigns duplicate archive member names stable ordinal identities including a later selected input', async () => {
    const archive = await tarFile([{ name: 'same.log', text: 'INFO one\n' }, { name: 'same.log', text: 'INFO two\n' }]);
    const later = new File(['INFO later\n'], 'later.log');
    const identities: { id: string; sourceIndex: number; entryIndex?: number }[] = [];
    for await (const source of readSources([archive, later], DEFAULT_LIMITS)) {
      identities.push({ id: source.id, sourceIndex: source.sourceIndex, entryIndex: source.entryIndex });
      if (source.chunks) for await (const _chunk of source.chunks) { /* drain */ }
    }
    expect(identities).toEqual([
      { id: 'source-0/entry-0', sourceIndex: 0, entryIndex: 0 },
      { id: 'source-0/entry-1', sourceIndex: 0, entryIndex: 1 },
      { id: 'source-1', sourceIndex: 1, entryIndex: undefined },
    ]);
  });

  it('queries only the requested later selected source and recognizes YAML metadata as text', async () => {
    const sources = [] as { id: string; sourceIndex: number; path: string }[];
    for await (const source of readSources([new File(['ignored\n'], 'first.log'), new File(['timezone: UTC\n'], 'robot.yaml')], DEFAULT_LIMITS, { targetId: 'source-1', stopAfterTarget: true })) {
      sources.push({ id: source.id, sourceIndex: source.sourceIndex, path: source.path });
      if (source.chunks) for await (const _chunk of source.chunks) { /* drain */ }
    }
    expect(sources).toEqual([{ id: 'source-1', sourceIndex: 1, path: 'robot.yaml' }]);
  });

  it('streams TAR.GZ log entries and inventories binary entries and links', async () => {
    const file = await tarFile([
      { name: 'node1/motor.log', text: '[2026-09-09 13:23:03.984211] [WARNING] [motor.cpp:15] drive timeout\n' },
      { name: 'robot.db3', text: 'SQLite format 3\0binary' },
      { name: 'latest.log', text: '', type: 'symlink' },
      { name: 'proc.log.WARNING.20260902.123', text: 'W0902 17:50:20.695700 5305 proc.cc:12] restart\n' },
    ]);
    const result = await preprocess([file], DEFAULT_LIMITS);
    expect(result.files.map(file => file.status)).toEqual(['processed', 'skipped', 'skipped', 'processed']);
    expect(result.totals.severityCounts.warn).toBe(2);
    expect(result.evidence[0].file).toBe('robot.tar.gz!/node1/motor.log');
  });

  it('rejects malformed gzip and keeps a later valid input', async () => {
    const result = await preprocess([new File(['invalid gzip'], 'bad.tar.gz'), new File(['ERROR motor failed\n'], 'ok.log')], DEFAULT_LIMITS);
    expect(result.files[0].status).toBe('error');
    expect(result.files.at(-1)?.status).toBe('processed');
    expect(result.totals.severityCounts.error).toBe(1);
  });

  it('rejects unsafe paths without extracting them', async () => {
    const result = await preprocess([await tarFile([{ name: '../unsafe.log', text: 'ERROR bad\n' }])], DEFAULT_LIMITS);
    expect(result.files[0].status).toBe('skipped');
    expect(result.files[0].reason).toContain('Unsafe');
  });

  it('records an archive entry limit instead of silently omitting the rest', async () => {
    const result = await preprocess([await tarFile([{ name: 'one.log', text: 'INFO one\n' }, { name: 'two.log', text: 'INFO two\n' }])], { ...DEFAULT_LIMITS, maxFiles: 1 });
    expect(result.files.some(file => file.status === 'error' && file.reason?.includes('limit'))).toBe(true);
  });

  it('enforces expanded-byte limits on skipped TAR bodies too', async () => {
    const result = await preprocess([await tarFile([{ name: 'robot.db3', text: 'x'.repeat(100000) }])], { ...DEFAULT_LIMITS, maxTotalExpandedBytes: 20000 });
    expect(result.files.some(file => file.status === 'error' && file.reason?.includes('limit'))).toBe(true);
  });

  it('streams ZIP entries with CRC checks', async () => {
    const writer = new ZipWriter(new BlobWriter('application/zip'));
    await writer.add('node/motor.log', new TextReader('ERROR drive lost\n'), { useWebWorkers: false });
    const file = new File([await writer.close()], 'robot.zip');
    const result = await preprocess([file], DEFAULT_LIMITS);
    expect(result.files[0].status).toBe('processed');
    expect(result.evidence[0].file).toBe('robot.zip!/node/motor.log');
  });

  it('does not accept a truncated TAR entry as complete', async () => {
    const original = await tarFile([{ name: 'motor.log', text: 'INFO drive\n'.repeat(100) }], false);
    const broken = new File([original.slice(0, 600)], 'broken.tar');
    const result = await preprocess([broken], DEFAULT_LIMITS);
    expect(result.files.some(file => file.status === 'partial' || file.status === 'error')).toBe(true);
  });
});
