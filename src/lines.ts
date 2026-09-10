export interface TextLine {
  line: number;
  text: string;
  originalChars: number;
  truncated: boolean;
  damaged: boolean;
}

/** Retains both ends of long lines and marks damaged text without discarding readable data. */
export async function* readLines(
  chunks: AsyncIterable<Uint8Array>,
  maxLineChars: number,
  onBytes?: (bytes: number) => void,
): AsyncGenerator<TextLine> {
  const cap = Math.max(1, Math.floor(maxLineChars));
  const headCap = Math.max(1, Math.ceil(cap * 0.75));
  const tailCap = cap - headCap;
  const decoder = new TextDecoder();
  let head = '';
  let tail = '';
  let chars = 0;
  let damaged = false;
  let line = 0;
  let lastChar = '';

  function append(part: string) {
    damaged ||= part.includes('\0') || part.includes('\uFFFD');
    chars += part.length;
    if (part) lastChar = part.slice(-1);
    if (head.length < headCap) head += part.slice(0, headCap - head.length);
    tail = (tail + part).slice(-(tailCap + 1)); // One extra character allows a terminal CR delimiter.
  }
  function finish(): TextLine {
    // CR is a delimiter only when it directly precedes LF/end-of-line.
    const originalChars = chars - (lastChar === '\r' ? 1 : 0);
    const truncated = originalChars > cap;
    let text: string;
    const cleanTail = lastChar === '\r' ? tail.slice(0, -1) : tail;
    if (originalChars <= headCap) text = head.slice(0, originalChars);
    else if (originalChars <= cap) text = head + cleanTail.slice(-(originalChars - headCap));
    else text = `${head}\n[... ${originalChars - cap} characters omitted ...]\n${tailCap ? cleanTail.slice(-tailCap) : ''}`;
    if (text.endsWith('\r')) text = text.slice(0, -1);
    text = text.replaceAll('\0', '\\0');
    const result = { line: ++line, text, originalChars, truncated, damaged };
    head = ''; tail = ''; chars = 0; damaged = false; lastChar = '';
    return result;
  }
  function* consume(text: string) {
    let start = 0;
    while (start < text.length) {
      const newline = text.indexOf('\n', start);
      if (newline < 0) { append(text.slice(start)); return; }
      append(text.slice(start, newline));
      yield finish();
      start = newline + 1;
    }
  }
  for await (const chunk of chunks) {
    onBytes?.(chunk.byteLength);
    yield* consume(decoder.decode(chunk, { stream: true }));
  }
  yield* consume(decoder.decode());
  if (chars) yield finish();
}
