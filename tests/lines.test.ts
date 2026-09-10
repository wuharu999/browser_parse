import { describe, expect, it } from 'vitest';
import { readLines } from '../src/lines';

const encode = new TextEncoder();
async function* chunks(...parts: string[]) { for (const part of parts) yield encode.encode(part); }

describe('bounded line replay', () => {
  it('does not count terminal CR as clipping at the exact character cap', async () => {
    const lines = [];
    for await (const line of readLines(chunks('12345678\r', '\n'), 8)) lines.push(line);
    expect(lines[0]).toMatchObject({ text: '12345678', originalChars: 8, truncated: false });
  });
  it('keeps exact line numbers, empty lines, CRLF and a final unterminated line', async () => {
    const lines = [];
    for await (const line of readLines(chunks('one\r', '\n\n', 'three'), 32)) lines.push(line);
    expect(lines.map(line => [line.line, line.text])).toEqual([[1, 'one'], [2, ''], [3, 'three']]);
  });
  it('preserves the first and last text on oversized lines regardless of chunks', async () => {
    const lines = [];
    for await (const line of readLines(chunks('ERROR motor 33 ', 'x'.repeat(10000), ' code=16\n'), 64)) lines.push(line);
    expect(lines[0].text).toContain('ERROR motor 33');
    expect(lines[0].text).toContain('code=16');
    expect(lines[0].truncated).toBe(true);
    expect(lines[0].text.length).toBeLessThan(130);
    expect(lines[0].originalChars).toBe(10023);
  });
  it('retains readable text around NUL padding with explicit damage flags', async () => {
    const lines = [];
    for await (const line of readLines(chunks('ERROR motor 33\0\0 code=16\n'), 64)) lines.push(line);
    expect(lines[0]).toMatchObject({ text: 'ERROR motor 33\\0\\0 code=16', damaged: true, truncated: false });
  });
  it('decodes split UTF8 without introducing replacement text', async () => {
    const data = encode.encode('电机33\n');
    const input = { async *[Symbol.asyncIterator]() { yield data.slice(0, 2); yield data.slice(2); } };
    const lines = [];
    for await (const line of readLines(input, 64)) lines.push(line);
    expect(lines[0]).toMatchObject({ text: '电机33', damaged: false });
  });
});
