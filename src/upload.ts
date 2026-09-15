export type UploadProgress = {
  loaded: number;
  total: number;
  bytesPerSecond: number;
  remainingSeconds: number | null;
  stalled: boolean;
  awaitingResponse: boolean;
};

/** PUT the original bytes; report transmission separately from server acceptance. */
export function uploadFile(url: string, file: Blob, token: string, signal: AbortSignal,
  onProgress: (progress: UploadProgress) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Upload cancelled', 'AbortError')); return; }
    const xhr = new XMLHttpRequest();
    let loaded = 0, lastByteAt = performance.now(), sent = false, settled = false;
    const started = lastByteAt;
    const emit = () => {
      const now = performance.now();
      const stalled = !sent && now - lastByteAt >= 10_000;
      const rate = stalled || sent ? 0 : loaded / Math.max((now - started) / 1000, 0.001);
      onProgress({ loaded, total: file.size, bytesPerSecond: rate,
        remainingSeconds: rate > 0 ? Math.ceil((file.size - loaded) / rate) : null,
        stalled, awaitingResponse: sent });
    };
    const cleanup = () => {
      clearInterval(timer);
      signal.removeEventListener('abort', abort);
      xhr.upload.onprogress = xhr.upload.onload = null;
      xhr.onload = xhr.onerror = xhr.onabort = xhr.ontimeout = null;
    };
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true; cleanup();
      if (error) reject(error); else resolve();
    };
    const abort = () => { xhr.abort(); finish(new DOMException('Upload cancelled', 'AbortError')); };
    const timer = setInterval(emit, 1000);
    // Register upload listeners before open/send for browser compatibility.
    xhr.upload.onprogress = event => {
      const next = Math.min(file.size, Math.max(loaded, event.loaded));
      if (next > loaded) lastByteAt = performance.now();
      loaded = next;
      if (loaded === file.size) sent = true;
      emit();
    };
    xhr.upload.onload = () => { sent = true; loaded = file.size; emit(); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { finish(); return; }
      let detail = `HTTP ${xhr.status}`;
      try { const body = JSON.parse(xhr.responseText); if (typeof body.detail === 'string') detail = body.detail; } catch { /* Keep HTTP status for non-JSON proxy errors. */ }
      finish(new Error(detail));
    };
    xhr.onerror = () => finish(new Error('Upload network error / 上传网络错误'));
    xhr.ontimeout = () => finish(new Error('Upload timed out / 上传超时'));
    xhr.onabort = () => finish(new DOMException('Upload cancelled', 'AbortError'));
    signal.addEventListener('abort', abort, { once: true });
    try {
      xhr.open('PUT', url);
      xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      emit(); xhr.send(file);
    } catch (error) { finish(error instanceof Error ? error : new Error('Upload failed')); }
  });
}

export type WakeState = 'requesting' | 'active' | 'unavailable';

/** Best-effort screen lock plus refresh protection for one submission lifetime. */
export function protectUpload(onWakeState: (state: WakeState) => void): () => void {
  let active = true, pending = false, lock: WakeLockSentinel | undefined;
  let visibilityVersion = 0;
  const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
  const request = async () => {
    if (!active || pending || lock || document.visibilityState !== 'visible') return;
    if (!window.isSecureContext || !navigator.wakeLock) { onWakeState('unavailable'); return; }
    pending = true; const requestedVersion = visibilityVersion; onWakeState('requesting');
    try {
      const next = await navigator.wakeLock.request('screen');
      if (!active || requestedVersion !== visibilityVersion || document.visibilityState !== 'visible') { await next.release(); return; }
      if (next.released) { onWakeState('unavailable'); return; }
      lock = next; onWakeState('active');
      next.addEventListener('release', () => {
        if (lock === next) { lock = undefined; if (active) onWakeState('unavailable'); }
      }, { once: true });
    } catch { if (active) onWakeState('unavailable'); }
    finally {
      pending = false;
      if (active && requestedVersion !== visibilityVersion && document.visibilityState === 'visible') void request();
    }
  };
  const visibility = () => {
    visibilityVersion++;
    if (document.visibilityState === 'visible') { void request(); return; }
    const previous = lock; lock = undefined;
    if (previous) void previous.release().catch(() => {});
    if (active) onWakeState('unavailable');
  };
  window.addEventListener('beforeunload', beforeUnload);
  document.addEventListener('visibilitychange', visibility);
  void request();
  return () => {
    active = false;
    window.removeEventListener('beforeunload', beforeUnload);
    document.removeEventListener('visibilitychange', visibility);
    if (lock) { void lock.release().catch(() => {}); lock = undefined; }
  };
}
