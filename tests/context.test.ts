import { describe, expect, it } from 'vitest';
import { readContext } from '../src/context';
import { DEFAULT_LIMITS } from '../src/types';
import { pack } from 'it-tar';

async function tarFile(entries: { name: string; text: string }[]): Promise<File> {
  const parts: Uint8Array[] = [];
  for await (const part of pack()(entries.map((entry) => ({ header: { name: entry.name }, body: entry.text })))) parts.push(part);
  return new File([new Blob(parts as BlobPart[])], 'robot.tar');
}

describe('browser-session source context', () => {
  it('reopens only the requested standalone source and bounds its returned lines', async () => {
    const files = [new File(['first\nsecond\nthird\nfourth\n'], 'drive.log'), new File(['other\n'], 'other.log')];
    const result = await readContext(files, DEFAULT_LIMITS, { sourceId: 'source-0', startLine: 2, maxLines: 2, maxBytes: 100 });
    expect(result).toMatchObject({ sourceId: 'source-0', path: 'drive.log', startLine: 2, truncated: true, nextLine: 4 });
    expect(result.lines.map((line) => [line.line, line.text])).toEqual([[2, 'second'], [3, 'third']]);
  });

  it('does not advertise a next page when a max-lines page ends exactly at EOF', async () => {
    const result = await readContext([new File(['one\ntwo\n'], 'drive.log')], DEFAULT_LIMITS, { sourceId: 'source-0', startLine: 1, maxLines: 2, maxBytes: 512 });
    expect(result.lines.map((line) => line.line)).toEqual([1, 2]);
    expect(result.truncated).toBe(false);
    expect(result.nextLine).toBeUndefined();
  });

  it('advertises the first unseen line only after bounded lookahead confirms another page', async () => {
    const result = await readContext([new File(['one\ntwo\nthree\n'], 'drive.log')], DEFAULT_LIMITS, { sourceId: 'source-0', startLine: 1, maxLines: 2, maxBytes: 512 });
    expect(result.lines.map((line) => line.line)).toEqual([1, 2]);
    expect(result.truncated).toBe(true);
    expect(result.nextLine).toBe(3);
  });

  it('returns a local availability warning for a stale source id', async () => {
    const result = await readContext([new File(['one\n'], 'drive.log')], DEFAULT_LIMITS, { sourceId: 'source-8', startLine: 1, maxLines: 2, maxBytes: 100 });
    expect(result.warning).toContain('no longer available');
  });

  it('retrieves a duplicate archive member by stable ordinal without changing original source bytes', async () => {
    const archive = await tarFile([{ name: 'same.log', text: 'first\n' }, { name: 'same.log', text: 'outside saved evidence\n' }]);
    const before = new Uint8Array(await archive.arrayBuffer());
    const result = await readContext([archive], DEFAULT_LIMITS, { sourceId: 'source-0/entry-1', startLine: 1, maxLines: 20, maxBytes: 12_288 });
    expect(result.lines.map((line) => line.text)).toEqual(['outside saved evidence']);
    expect(new Uint8Array(await archive.arrayBuffer())).toEqual(before);
  });

  it('bounds context response lines and bytes while returning a usable first excerpt', async () => {
    const result = await readContext([new File(['x'.repeat(3000) + '\nnext\n'], 'drive.log')], DEFAULT_LIMITS, { sourceId: 'source-0', startLine: 1, maxLines: 2000, maxBytes: 512 });
    expect(result.lines).toHaveLength(1);
    expect(result.lines[0].truncated).toBe(true);
    expect(result.truncated).toBe(true);
    expect(new TextEncoder().encode(JSON.stringify(result)).byteLength).toBeLessThanOrEqual(512);
  });

  it('bounds escaped Unicode context, long metadata, and non-finite query values', async () => {
    const longName = `${'path/'.repeat(700)}robot.log`;
    const result = await readContext([new File([`${'😀"\\'.repeat(500)}\nfinal\n`], longName)], DEFAULT_LIMITS, {
      sourceId: 'source-0', startLine: Number.NaN, maxLines: Number.POSITIVE_INFINITY, maxBytes: 512,
    });
    expect(result.startLine).toBe(1);
    expect(result.truncated).toBe(false);
    expect(result.warning).toContain('clipped');
    expect(new TextEncoder().encode(JSON.stringify(result)).byteLength).toBeLessThanOrEqual(512);
  });

  it('clips escaped multibyte line content at both ends without exceeding its JSON budget', async () => {
    const text = `BEGIN-${'😀"\\'.repeat(500)}-END\n`;
    const result = await readContext([new File([text], 'unicode.log')], DEFAULT_LIMITS, {
      sourceId: 'source-0', startLine: 1, maxLines: 1, maxBytes: 512,
    });
    expect(result.lines[0].text).toContain('BEGIN-');
    expect(result.lines[0].text).toContain('-END');
    expect(result.lines[0].truncated).toBe(true);
    expect(new TextEncoder().encode(JSON.stringify(result)).byteLength).toBeLessThanOrEqual(512);
  });

  it('enforces the per-source expanded-byte cap during a gzip context rescan', async () => {
    const plain = new Blob(['line\n'.repeat(1000)]);
    const gzip = await new Response(plain.stream().pipeThrough(new CompressionStream('gzip'))).blob();
    const result = await readContext([new File([gzip], 'drive.log.gz')], { ...DEFAULT_LIMITS, maxExpandedBytesPerFile: 128 }, {
      sourceId: 'source-0', startLine: 1, maxLines: 20, maxBytes: 512,
    });
    expect(result.warning).toContain('Per-file expanded-byte limit');
    expect(result.truncated).toBe(true);
    expect(new TextEncoder().encode(JSON.stringify(result)).byteLength).toBeLessThanOrEqual(512);
  });

  it('keeps a not-found response bounded even for an untrusted oversized source id', async () => {
    const result = await readContext([], DEFAULT_LIMITS, { sourceId: 'source-'.repeat(1000), startLine: 1, maxLines: 1, maxBytes: 512 });
    expect(new TextEncoder().encode(JSON.stringify(result)).byteLength).toBeLessThanOrEqual(512);
  });
});
