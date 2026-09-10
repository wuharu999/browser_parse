import { readLines } from './lines';
import { readSources } from './sources';
import type { ContextQuery, ContextResult, Limits } from './types';

type ContextLine = ContextResult['lines'][number];
const encoder = new TextEncoder();
const clipMarker = ' … [excerpt clipped] … ';

function encodedBytes(value: unknown): number { return encoder.encode(JSON.stringify(value)).byteLength; }

function boundedNumber(value: number, fallback: number, minimum: number, maximum: number): number {
  return Number.isFinite(value) ? Math.min(maximum, Math.max(minimum, Math.floor(value))) : fallback;
}

/** UTF-8-bounded excerpt that retains both the beginning and end when possible. */
function clipMiddle(text: string, maxBytes: number): string {
  if (encoder.encode(text).byteLength <= maxBytes) return text;
  const chars = Array.from(text);
  const candidate = (count: number) => {
    if (count >= chars.length) return text;
    if (count <= 0) return '';
    const head = Math.ceil(count / 2);
    const tail = Math.floor(count / 2);
    return tail === 0 ? chars.slice(0, head).join('') : `${chars.slice(0, head).join('')}${clipMarker}${chars.slice(-tail).join('')}`;
  };
  let low = 0;
  let high = chars.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (encoder.encode(candidate(middle)).byteLength <= maxBytes) low = middle;
    else high = middle - 1;
  }
  return candidate(low);
}

/** Fits a line against its actual serialized representation without quadratic trimming. */
function fitLine(line: ContextLine, availableBytes: number): ContextLine | undefined {
  if (encodedBytes(line) <= availableBytes) return line;
  const chars = Array.from(line.text);
  const fromCount = (count: number): ContextLine => {
    if (count >= chars.length) return { ...line, truncated: true };
    if (count <= 0) return { ...line, text: '', truncated: true };
    const head = Math.ceil(count / 2);
    const tail = Math.floor(count / 2);
    return { ...line, text: tail === 0 ? chars.slice(0, head).join('') : `${chars.slice(0, head).join('')}${clipMarker}${chars.slice(-tail).join('')}`, truncated: true };
  };
  let low = 0;
  let high = chars.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (encodedBytes(fromCount(middle)) <= availableBytes) low = middle;
    else high = middle - 1;
  }
  const result = fromCount(low);
  return encodedBytes(result) <= availableBytes ? result : undefined;
}

/** Last defense: every success, unavailable-source, and error response fits the caller's serialized JSON budget. */
function fitResult(result: ContextResult, maxBytes: number): ContextResult {
  const output: ContextResult = { ...result, lines: [...result.lines] };
  let firstDroppedLine: number | undefined;
  while (encodedBytes(output) > maxBytes && output.lines.length > 0) {
    const dropped = output.lines.pop();
    firstDroppedLine = dropped?.line;
    output.truncated = true;
  }
  // If final response fitting removed an otherwise selected line, resume exactly at
  // that first absent line instead of retaining a nextLine calculated pre-fitting.
  if (firstDroppedLine !== undefined) output.nextLine = firstDroppedLine;
  const originalPath = output.path;
  const originalSourceId = output.sourceId;
  const fitProperty = (key: 'sourceId' | 'path' | 'warning') => {
    const value = output[key];
    if (!value || encodedBytes(output) <= maxBytes) return;
    const chars = Array.from(value);
    const candidate = (count: number) => count >= chars.length ? value : clipMiddle(value, encoder.encode(chars.slice(0, count).join('')).byteLength);
    let low = 0;
    let high = chars.length;
    while (low < high) {
      const middle = Math.ceil((low + high) / 2);
      output[key] = candidate(middle);
      if (encodedBytes(output) <= maxBytes) low = middle;
      else high = middle - 1;
    }
    output[key] = candidate(low);
  };
  fitProperty('sourceId');
  fitProperty('path');
  if (output.path !== originalPath || output.sourceId !== originalSourceId) output.warning = output.warning ? `Metadata clipped: ${output.warning}` : 'Source metadata was clipped to keep this local context response bounded.';
  fitProperty('warning');
  // The minimum response cap leaves room for fixed schema. This covers pathological escaping/metadata combinations.
  while (encodedBytes(output) > maxBytes && output.warning) output.warning = output.warning.slice(0, -1);
  if (output.warning === '') output.warning = undefined;
  return output;
}

function unavailable(sourceId: string, text: string, maxBytes: number): ContextResult {
  return fitResult({ sourceId, path: '', startLine: 1, lines: [], truncated: false, warning: text, integrity: 'not-revalidated' }, maxBytes);
}

/**
 * Reopens one user-selected source in this browser session and stops once the requested line window is bounded.
 * A TAR must be scanned sequentially to the member, but ZIP data for non-target members is never expanded.
 */
export async function readContext(files: File[], limits: Limits, query: ContextQuery): Promise<ContextResult> {
  const startLine = boundedNumber(query.startLine, 1, 1, Number.MAX_SAFE_INTEGER);
  const maxLines = boundedNumber(query.maxLines, 20, 1, 200);
  const maxBytes = boundedNumber(query.maxBytes, 12_288, 512, 65_536);

  for await (const source of readSources(files, limits, { targetId: query.sourceId, stopAfterTarget: true })) {
    if (!source.chunks) return fitResult({
      sourceId: source.id, path: source.path, startLine, lines: [], truncated: false,
      warning: source.reason ?? 'This source has no readable text stream.', integrity: 'not-revalidated',
    }, maxBytes);

    const lines: ContextLine[] = [];
    let outputBytes = 0;
    let stopped = false;
    let limitReached = false;
    let lineClipped = false;
    let moreAfterWindow = false;
    let firstUnreturnedLine: number | undefined;
    let sourceBytes = 0;
    const responseOverhead = encodedBytes({ sourceId: source.id, path: source.path, startLine, lines: [], truncated: false, integrity: 'not-revalidated' }) + 96;
    try {
      for await (const textLine of readLines(source.chunks, Math.min(limits.maxLineChars, maxBytes), (chunkBytes) => {
        sourceBytes += chunkBytes;
        if (sourceBytes > limits.maxExpandedBytesPerFile) throw new Error('Per-file expanded-byte limit reached while retrieving context.');
      })) {
        if (textLine.line < startLine) continue;
        // A single lookahead distinguishes an exact-EOF final page from a page
        // that merely filled its line/byte window.
        if (limitReached) {
          moreAfterWindow = true;
          break;
        }
        const available = maxBytes - responseOverhead - outputBytes;
        if (available <= 0) { stopped = true; firstUnreturnedLine = textLine.line; break; }
        const line = fitLine(textLine, available);
        if (!line) { stopped = true; firstUnreturnedLine = textLine.line; break; }
        outputBytes += encodedBytes(line) + 1;
        lines.push(line);
        if (line.truncated) lineClipped = true;
        if (line.truncated || lines.length >= maxLines) limitReached = true;
      }
    } catch (error) {
      return fitResult({
        sourceId: source.id, path: source.path, startLine, lines, truncated: true,
        nextLine: lines.length > 0 ? lines[lines.length - 1].line + 1 : startLine,
        warning: error instanceof Error ? error.message : String(error), integrity: 'not-revalidated',
      }, maxBytes);
    }
    return fitResult({
      sourceId: source.id, path: source.path, startLine, lines,
      nextLine: firstUnreturnedLine ?? (moreAfterWindow && lines.length > 0 ? lines[lines.length - 1].line + 1 : undefined),
      truncated: stopped || moreAfterWindow || lineClipped,
      integrity: 'not-revalidated',
    }, maxBytes);
  }
  return unavailable(query.sourceId, 'The requested source is no longer available in the current browser selection.', maxBytes);
}
