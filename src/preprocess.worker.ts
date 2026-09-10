/// <reference lib="webworker" />
import { preprocess } from './preprocess';
import { readContext } from './context';
import type { WorkerRequest, WorkerResponse } from './types';

type WorkerScope = {
  postMessage(message: WorkerResponse): void;
  onmessage: ((event: MessageEvent<WorkerRequest>) => void) | null;
};
// `self` is typed as Window when the app's DOM lib is also in tsconfig.
const scope = self as unknown as WorkerScope;
scope.onmessage = (event: MessageEvent<WorkerRequest>) => {
  if (event.data.type === 'start') {
    void preprocess(event.data.files, event.data.limits, (progress) => {
      scope.postMessage({ type: 'progress', progress } satisfies WorkerResponse);
    }).then((result) => {
      scope.postMessage({ type: 'complete', result } satisfies WorkerResponse);
    }).catch((error: unknown) => {
      scope.postMessage({ type: 'error', message: error instanceof Error ? error.message : String(error) } satisfies WorkerResponse);
    });
    return;
  }
  if (event.data.type === 'context') {
    const { requestId } = event.data;
    void readContext(event.data.files, event.data.limits, event.data.query).then((result) => {
      scope.postMessage({ type: 'context', requestId, result } satisfies WorkerResponse);
    }).catch((error: unknown) => {
      scope.postMessage({ type: 'error', requestId, message: error instanceof Error ? error.message : String(error) } satisfies WorkerResponse);
    });
  }
};
