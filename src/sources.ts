import { extract } from 'it-tar';
import { BlobReader, ZipReader } from '@zip.js/zip.js';
import type { Limits } from './types';

export interface LogSource {
  /** Stable identity derived only from the original file-selection order and archive member ordinal. */
  id: string;
  sourceIndex: number;
  /** Present for TAR/ZIP members; includes directories and unsupported members in its ordinal. */
  entryIndex?: number;
  /** Relative path of the selected source file, before an archive member is appended to `path`. */
  sourcePath: string;
  path: string;
  sizeBytes: number;
  chunks?: AsyncIterable<Uint8Array>;
  status?: 'skipped' | 'error' | 'partial';
  reason?: string;
}

export interface ReadSourcesOptions {
  /** Re-open only this stable source. ZIP avoids expanding non-target members; TAR streams to it sequentially. */
  targetId?: string;
  /** Return immediately after the target is yielded; intended for bounded context reads. */
  stopAfterTarget?: boolean;
}

async function* streamChunks(stream: ReadableStream<Uint8Array>): AsyncGenerator<Uint8Array> {
  const reader = stream.getReader();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      yield value;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

const textFile = (path: string) => /(?:\.(?:log|txt|jsonl|ndjson|json|yaml|yml|ini|conf|cfg)(?:\.|$)|\.(?:INFO|WARNING|ERROR|FATAL)(?:\.|$)|(?:^|\/)(?:syslog|messages|dmesg)(?:\.|$))/i.test(path);
const unsafePath = (path: string) => path.length > 1024 || /[\x00-\x1f\\]/.test(path) || /^(?:\/|[A-Za-z]:)/.test(path) || path.split('/').includes('..');
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

/** Reads bytes only as each consumer requests them; nothing is extracted to disk. */
export async function* readSources(files: File[], limits: Limits, options: ReadSourcesOptions = {}): AsyncGenerator<LogSource> {
  let expanded = 0;
  let entries = 0;
  let stopped = false;
  function charge(bytes: number) {
    expanded += bytes;
    if (expanded > limits.maxTotalExpandedBytes) {
      stopped = true;
      throw new Error('Total expanded-byte limit reached; remaining input was not read.');
    }
  }
  async function* metered(stream: ReadableStream<Uint8Array>) {
    for await (const chunk of streamChunks(stream)) {
      charge(chunk.byteLength);
      yield chunk;
    }
  }
  async function* tarChunks(stream: ReadableStream<Uint8Array>) {
    let total = 0;
    let tail = new Uint8Array(0);
    for await (const chunk of metered(stream)) {
      total += chunk.length;
      const nextTail = new Uint8Array(Math.min(1024, tail.length + chunk.length));
      if (chunk.length >= 1024) nextTail.set(chunk.subarray(-1024));
      else {
        const kept = Math.min(tail.length, 1024 - chunk.length);
        nextTail.set(tail.subarray(tail.length - kept));
        nextTail.set(chunk, kept);
      }
      tail = nextTail;
      yield chunk;
    }
    if (total % 512 !== 0 || tail.length < 1024 || tail.some(byte => byte !== 0)) {
      throw new Error('TAR is truncated or missing its two end-of-archive blocks.');
    }
  }
  function reserve() {
    entries++;
    if (entries > limits.maxFiles) {
      stopped = true;
      throw new Error('Archive entry/file limit reached; remaining input was not read.');
    }
  }
  function reason(name: string, size: number, type = 'file'): string | undefined {
    if (unsafePath(name)) return 'Unsafe or overlong archive path; entry ignored.';
    if (type !== 'file' && type !== 'contiguous-file') return `Unsupported entry type: ${type}; links are not followed.`;
    if (size > limits.maxExpandedBytesPerFile) return 'Declared entry size exceeds the per-file expanded-byte limit.';
    if (!textFile(name)) return 'Unsupported format; binary recordings and nested archives require a separate adapter.';
  }

  for (let index = 0; index < files.length; index++) {
    const file = files[index];
    const path = file.webkitRelativePath || file.name;
    const sourceId = `source-${index}`;
    const sourceFields = { sourceIndex: index, sourcePath: path };
    if (stopped || index >= limits.maxFiles) {
      if (!options.targetId || options.targetId === sourceId) yield { id: sourceId, ...sourceFields, path: '[unread selection]', sizeBytes: 0, status: 'skipped', reason: `${files.length - index} selected files were not read because a resource limit was reached.` };
      return;
    }
    try {
      if (unsafePath(path)) {
        reserve();
        if (!options.targetId || options.targetId === sourceId) yield { id: sourceId, ...sourceFields, path: path.slice(0, 1024), sizeBytes: file.size, status: 'skipped', reason: 'Unsafe or overlong source path.' };
        continue;
      }
      if (/\.(?:tar\.gz|tgz|tar)$/i.test(path)) {
        if (options.targetId && !options.targetId.startsWith(`${sourceId}/entry-`)) continue;
        const stream = /\.(?:gz|tgz)$/i.test(path)
          ? file.stream().pipeThrough(new DecompressionStream('gzip'))
          : file.stream();
        let entryIndex = 0;
        for await (const entry of extract()(tarChunks(stream))) {
          const currentEntryIndex = entryIndex++;
          const entryId = `${sourceId}/entry-${currentEntryIndex}`;
          reserve();
          const header = entry.header;
          if (header.type === 'directory') {
            for await (const _chunk of entry.body) { /* drain */ }
            continue;
          }
          const target = options.targetId === entryId;
          if (options.targetId && !target) {
            for await (const _chunk of entry.body) { /* TAR must drain earlier members. */ }
            continue;
          }
          const entryPath = `${path}!/${header.name}`;
          const skip = reason(header.name, header.size ?? 0, header.type);
          // Keep the body iterator alive when the consumer stops at its own limit.
          const iterator = entry.body[Symbol.asyncIterator]();
          let bodyBytes = 0;
          async function* body() {
            let complete = false;
            try {
              while (true) {
                const next = await iterator.next();
                if (next.done) {
                  complete = true;
                  if (bodyBytes !== header.size) throw new Error('TAR entry body is truncated or has an invalid declared size.');
                  return;
                }
                const chunk = next.value.subarray();
                bodyBytes += chunk.length;
                yield chunk;
              }
            } finally {
              // Context reads may stop mid-member. Closing this iterator releases the TAR parser rather than retaining it.
              if (!complete) await iterator.return?.();
            }
          }
          if (skip) yield { id: entryId, ...sourceFields, entryIndex: currentEntryIndex, path: entryPath.slice(0, 2100), sizeBytes: header.size ?? 0, status: 'skipped', reason: skip };
          else yield { id: entryId, ...sourceFields, entryIndex: currentEntryIndex, path: entryPath, sizeBytes: header.size ?? 0, chunks: body() };
          if (target && options.stopAfterTarget) return;
          // TAR is sequential; advance past even unsupported or partially read entries.
          while (true) {
            const next = await iterator.next();
            if (next.done) break;
            bodyBytes += next.value.length;
          }
          if (bodyBytes !== header.size) throw new Error('TAR entry body is truncated or has an invalid declared size.');
        }
      } else if (/\.zip$/i.test(path)) {
        if (options.targetId && !options.targetId.startsWith(`${sourceId}/entry-`)) continue;
        const reader = new ZipReader(new BlobReader(file));
        try {
          let entryIndex = 0;
          for await (const entry of reader.getEntriesGenerator()) {
            const currentEntryIndex = entryIndex++;
            const entryId = `${sourceId}/entry-${currentEntryIndex}`;
            reserve();
            if (entry.directory) continue;
            const fileEntry = entry;
            const entryPath = `${path}!/${entry.filename}`;
            const target = options.targetId === entryId;
            if (options.targetId && !target) continue;
            const skip = entry.encrypted ? 'Encrypted ZIP entries are unsupported.' : reason(entry.filename, entry.uncompressedSize);
            if (skip) {
              yield { id: entryId, ...sourceFields, entryIndex: currentEntryIndex, path: entryPath.slice(0, 2100), sizeBytes: entry.uncompressedSize, status: 'skipped', reason: skip };
              if (target && options.stopAfterTarget) return;
              continue;
            }
            async function* body() {
              const stream = new TransformStream<Uint8Array, Uint8Array>();
              let failure: unknown;
              const writing = fileEntry.getData(stream.writable, { checkSignature: true, useWebWorkers: false }).catch((error: unknown) => { failure = error; });
              let fileBytes = 0;
              try {
                for await (const chunk of streamChunks(stream.readable)) {
                  charge(chunk.byteLength);
                  fileBytes += chunk.byteLength;
                  if (fileBytes > limits.maxExpandedBytesPerFile) throw new Error('Per-file expanded-byte limit reached.');
                  yield chunk;
                }
                await writing;
                if (failure) throw failure;
              } finally {
                await writing;
              }
            }
            yield { id: entryId, ...sourceFields, entryIndex: currentEntryIndex, path: entryPath, sizeBytes: entry.uncompressedSize, chunks: body() };
            if (target && options.stopAfterTarget) return;
          }
        } finally {
          await reader.close();
        }
      } else {
        if (options.targetId && options.targetId !== sourceId) continue;
        reserve();
        const gzip = /\.gz$/i.test(path);
        const skip = reason(gzip ? path.slice(0, -3) : path, gzip ? 0 : file.size);
        if (skip) yield { id: sourceId, ...sourceFields, path, sizeBytes: file.size, status: 'skipped', reason: skip };
        else {
          const stream = gzip ? file.stream().pipeThrough(new DecompressionStream('gzip')) : file.stream();
          yield { id: sourceId, ...sourceFields, path, sizeBytes: file.size, chunks: metered(stream) };
        }
        if (options.targetId === sourceId && options.stopAfterTarget) return;
      }
    } catch (error) {
      if (!options.targetId || options.targetId === sourceId || options.targetId.startsWith(`${sourceId}/entry-`)) yield { id: sourceId, ...sourceFields, path, sizeBytes: file.size, status: 'error', reason: `Input could not be fully read: ${errorText(error)}` };
    }
  }
}
