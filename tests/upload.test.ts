import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { protectUpload, uploadFile, type UploadProgress } from '../src/upload';

class FakeXHR {
  static latest: FakeXHR;
  upload = { onprogress: null as ((event: { loaded: number }) => void) | null, onload: null as (() => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  status = 200;
  responseText = '';
  headers: Record<string, string> = {};
  body?: Blob;
  aborted = false;
  constructor() { FakeXHR.latest = this; }
  open = vi.fn();
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send(body: Blob) { this.body = body; }
  abort() { this.aborted = true; this.onabort?.(); }
}

beforeEach(() => { vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance'] }); vi.stubGlobal('XMLHttpRequest', FakeXHR); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('byte uploads', () => {
  it('sends raw bytes with auth, detects a 10-second stall, and waits for HTTP acceptance at 100%', async () => {
    const updates: UploadProgress[] = [], file = new Blob(['x'.repeat(1000)]);
    const request = uploadFile('/api/jobs/test/files?name=a.log', file, 'test-only-token', new AbortController().signal, p => updates.push(p));
    const xhr = FakeXHR.latest;
    expect(xhr.open).toHaveBeenCalledWith('PUT', '/api/jobs/test/files?name=a.log');
    expect(xhr.body).toBe(file);
    expect(xhr.headers).toEqual({ Authorization: 'Bearer test-only-token', 'Content-Type': 'application/octet-stream' });
    vi.advanceTimersByTime(1000); xhr.upload.onprogress?.({ loaded: 400 });
    expect(updates.at(-1)).toMatchObject({ loaded: 400, total: 1000, bytesPerSecond: 400, remainingSeconds: 2, stalled: false });
    vi.advanceTimersByTime(9000);
    expect(updates.at(-1)?.stalled).toBe(false);
    // Duplicate byte counts do not restart the stall timer.
    xhr.upload.onprogress?.({ loaded: 400 }); vi.advanceTimersByTime(1000);
    expect(updates.at(-1)).toMatchObject({ stalled: true, bytesPerSecond: 0, remainingSeconds: null });
    xhr.upload.onprogress?.({ loaded: 700 });
    expect(updates.at(-1)?.stalled).toBe(false);
    xhr.upload.onload?.();
    expect(updates.at(-1)).toMatchObject({ loaded: 1000, awaitingResponse: true, stalled: false });
    let resolved = false; void request.then(() => { resolved = true; });
    await Promise.resolve(); expect(resolved).toBe(false);
    vi.advanceTimersByTime(20_000); expect(updates.at(-1)?.stalled).toBe(false);
    xhr.onload?.(); await request;
    expect(vi.getTimerCount()).toBe(0);
    expect(xhr.upload.onprogress).toBeNull();
  });

  it.each(['error', 'http', 'abort', 'timeout'])('cleans up on %s and preserves failure', async kind => {
    const controller = new AbortController();
    const request = uploadFile('/files', new Blob(['abc']), 'test-token', controller.signal, () => {});
    const failure = expect(request).rejects.toThrow();
    const xhr = FakeXHR.latest;
    if (kind === 'error') xhr.onerror?.();
    if (kind === 'http') { xhr.status = 507; xhr.responseText = '{"detail":"insufficient upload storage"}'; xhr.onload?.(); }
    if (kind === 'abort') controller.abort();
    if (kind === 'timeout') xhr.ontimeout?.();
    await failure;
    expect(vi.getTimerCount()).toBe(0);
    expect(xhr.onload).toBeNull();
  });

  it('rejects an already-aborted request without starting any transfer', async () => {
    const controller = new AbortController(); controller.abort();
    await expect(uploadFile('/files', new Blob(), '', controller.signal, () => {})).rejects.toMatchObject({ name: 'AbortError' });
    expect(vi.getTimerCount()).toBe(0);
  });
});

class ScreenLock extends EventTarget {
  release = vi.fn(async () => { this.dispatchEvent(new Event('release')); });
}
function browser(secure = true) {
  const win = Object.assign(new EventTarget(), { isSecureContext: secure });
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' });
  const request = vi.fn(async () => new ScreenLock());
  vi.stubGlobal('window', win); vi.stubGlobal('document', doc); vi.stubGlobal('navigator', { wakeLock: { request } });
  return { win, doc, request };
}

describe('submission lifetime protection', () => {
  it('protects reload only while active and reacquires a released screen lock when visible again', async () => {
    const { win, doc, request } = browser(), state = vi.fn();
    const stop = protectUpload(state); await Promise.resolve();
    expect(state).toHaveBeenLastCalledWith('active');
    const attempt = new Event('beforeunload', { cancelable: true });
    expect(win.dispatchEvent(attempt)).toBe(false);
    const first = await request.mock.results[0].value;
    doc.visibilityState = 'hidden'; await first.release();
    doc.dispatchEvent(new Event('visibilitychange')); expect(request).toHaveBeenCalledTimes(1);
    doc.visibilityState = 'visible'; doc.dispatchEvent(new Event('visibilitychange')); await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(2);
    const second = await request.mock.results[1].value;
    stop(); expect(second.release).toHaveBeenCalledTimes(1);
    expect(win.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(true);
    doc.dispatchEvent(new Event('visibilitychange')); expect(request).toHaveBeenCalledTimes(2);
  });

  it('explicitly releases on hiding and handles a pending lock across hide/show', async () => {
    const { doc, request } = browser();
    const stop = protectUpload(() => {}); await Promise.resolve();
    const first = await request.mock.results[0].value;
    doc.visibilityState = 'hidden'; doc.dispatchEvent(new Event('visibilitychange'));
    expect(first.release).toHaveBeenCalledTimes(1);
    doc.visibilityState = 'visible'; doc.dispatchEvent(new Event('visibilitychange')); await Promise.resolve();
    expect(request).toHaveBeenCalledTimes(2); stop();

    const nextBrowser = browser();
    let complete!: (lock: ScreenLock) => void;
    nextBrowser.request.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    const done = protectUpload(() => {});
    nextBrowser.doc.visibilityState = 'hidden'; nextBrowser.doc.dispatchEvent(new Event('visibilitychange'));
    nextBrowser.doc.visibilityState = 'visible'; nextBrowser.doc.dispatchEvent(new Event('visibilitychange'));
    const stale = new ScreenLock(); complete(stale);
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(stale.release).toHaveBeenCalledTimes(1);
    expect(nextBrowser.request).toHaveBeenCalledTimes(2); done();
  });

  it('allows uploading without a secure context or when the OS rejects the lock', async () => {
    const insecure = browser(false), state = vi.fn();
    const stop = protectUpload(state);
    expect(state).toHaveBeenLastCalledWith('unavailable'); expect(insecure.request).not.toHaveBeenCalled(); stop();
    const secure = browser(); secure.request.mockRejectedValue(new Error('low battery'));
    const done = protectUpload(state); await Promise.resolve();
    expect(state).toHaveBeenLastCalledWith('unavailable'); done();
  });

  it('releases a lock that resolves after upload cancellation', async () => {
    const { request } = browser();
    let complete!: (lock: ScreenLock) => void;
    request.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    const state = vi.fn(), stop = protectUpload(state);
    stop(); const lock = new ScreenLock(); complete(lock); await Promise.resolve();
    expect(lock.release).toHaveBeenCalledTimes(1);
    expect(state).not.toHaveBeenCalledWith('active');
  });
});
