import './style.css';
import { buildBrief } from './brief';
import { mountDashboard } from './dashboard';
import { onUiLanguage, setUiLanguage, setUiText, translateTree, uiLanguage } from './i18n';
import { DEFAULT_LIMITS, type Evidence, type FileReport, type LogPackage, type Progress, type Severity, type WorkerRequest, type WorkerResponse } from './types';

const app = document.querySelector<HTMLDivElement>('#app');
if (!app) throw new Error('Application root is missing.');

const severityOrder: Severity[] = ['fatal', 'error', 'warn', 'info', 'debug', 'unknown'];
const rowLimit = 100;
let selectedFiles: File[] = [];
let worker: Worker | undefined;
let runId = 0;
let result: LogPackage | undefined;
let running = false;
let startedAt: number | undefined;
let contextWorker: Worker | undefined;
let contextRequestId = 0;
let briefJson: string | undefined;
let contextJson: string | undefined;
let contextNextLine: number | undefined;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text: string, className = 'button'): HTMLButtonElement {
  const node = el('button', className, text);
  node.type = 'button';
  return node;
}

function bytes(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  let amount = value;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

function nodeText(value: unknown): string { return value === undefined || value === null || value === '' ? '—' : String(value); }

const shell = el('main', 'shell');
const header = el('header', 'hero');
const eyebrow = el('p', 'eyebrow', 'LOCAL LOG PREPROCESSOR');
const title = el('h1', undefined, 'Robot log workbench');
const subtitle = el('p', 'hero-copy', 'Prepare logs locally, download a compact analysis brief, and retrieve more context when needed. No model calls or tokens are used during preprocessing.');
const uiLanguagePicker = button(uiLanguage() === 'zh' ? 'English' : '中文', 'button ui-language');
uiLanguagePicker.dataset.i18nSkip = ''; uiLanguagePicker.setAttribute('aria-label', 'Switch interface language / 切换界面语言'); uiLanguagePicker.setAttribute('aria-pressed', String(uiLanguage() === 'zh'));
uiLanguagePicker.addEventListener('click', () => setUiLanguage(uiLanguage() === 'zh' ? 'en' : 'zh'));
header.append(eyebrow, title, subtitle, uiLanguagePicker);

const controls = el('section', 'panel controls-panel');
controls.setAttribute('aria-labelledby', 'input-heading');
const controlsHead = el('div', 'section-heading');
const inputHeading = el('h2', undefined, '1. Upload compressed logs'); inputHeading.id = 'input-heading';
controlsHead.append(el('div', undefined), inputHeading);
controlsHead.firstElementChild?.append(el('p', 'section-kicker', 'INPUT'));
const localNote = el('p', 'local-note', 'Original files stay local during preprocessing. Creating shared analysis uploads selected originals and evidence to the backend.');
controlsHead.append(localNote);

const pickerRow = el('div', 'picker-row');
const folderInput = el('input') as HTMLInputElement;
folderInput.type = 'file'; folderInput.multiple = true; folderInput.setAttribute('webkitdirectory', ''); folderInput.setAttribute('directory', '');
folderInput.setAttribute('aria-label', 'Select a local folder');
const folderLabel = el('label', 'picker-button', 'Choose folder'); folderLabel.htmlFor = 'folder-input'; folderInput.id = 'folder-input'; folderLabel.append(folderInput);
const filesInput = el('input') as HTMLInputElement;
filesInput.type = 'file'; filesInput.multiple = true; filesInput.id = 'files-input'; filesInput.setAttribute('aria-label', 'Select local files or archives');
const filesLabel = el('label', 'picker-button secondary', 'Add files or archives'); filesLabel.htmlFor = 'files-input'; filesLabel.append(filesInput);
pickerRow.append(folderLabel, filesLabel);
const selection = el('p', 'selection', 'No files selected.');
const formatNote = el('p', 'format-note', 'Reads TAR.GZ/TGZ/TAR, GZ, ZIP, text logs and configuration metadata. Binary MCAP/ROS recordings are inventoried as unsupported.');
const actionRow = el('div', 'action-row');
const startButton = button('Preprocess logs', 'button primary');
const cancelButton = button('Cancel run', 'button danger'); cancelButton.disabled = true;
const exportButton = button('Export JSON', 'button'); exportButton.disabled = true;
const briefButton = button('Download analysis brief', 'button primary'); briefButton.disabled = true;
const exportNote = el('p', 'format-note', 'The brief is capped at 16 KB, not an exact token count. Export JSON includes the larger local evidence package.');
actionRow.append(startButton, cancelButton, briefButton, exportButton);
controls.append(controlsHead, pickerRow, selection, formatNote, actionRow);
controls.append(exportNote);

const statusPanel = el('section', 'panel status-panel');
statusPanel.setAttribute('aria-labelledby', 'status-heading');
const statusHeading = el('div', 'section-heading');
const statusTitle = el('h2', undefined, '2. Run status'); statusTitle.id = 'status-heading';
statusHeading.append(el('div', undefined), statusTitle);
statusHeading.firstElementChild?.append(el('p', 'section-kicker', 'WORKER'));
const runState = el('p', 'run-state', 'Ready for local input.'); runState.setAttribute('role', 'status'); runState.setAttribute('aria-live', 'polite');
const progressBar = el('div', 'progress-track'); progressBar.setAttribute('role', 'progressbar'); progressBar.setAttribute('aria-label', 'Preprocessing progress'); progressBar.setAttribute('aria-valuetext', 'No active preprocessing run');
const progressValue = el('div', 'progress-value'); progressBar.append(progressValue);
const progressMeta = el('p', 'progress-meta', 'No active file.');
statusPanel.append(statusHeading, runState, progressBar, progressMeta);

const resultsPanel = el('section', 'panel results-panel');
resultsPanel.setAttribute('aria-labelledby', 'results-heading');
const resultsHeading = el('div', 'section-heading');
const resultsTitle = el('h2', undefined, '3. Bounded results'); resultsTitle.id = 'results-heading';
resultsHeading.append(el('div', undefined), resultsTitle);
resultsHeading.firstElementChild?.append(el('p', 'section-kicker', 'EVIDENCE'));
const resultSummary = el('p', 'empty-state', 'Results will appear after a successful local preprocessing run.');
const metrics = el('div', 'metrics');
const warningArea = el('div', 'warning-area');
const coverage = el('div', 'result-block');
const evidence = el('div', 'result-block');
const patterns = el('div', 'result-block');
resultsPanel.append(resultsHeading, resultSummary, metrics, warningArea, coverage, evidence, patterns);

const contextPanel = el('section', 'panel');
contextPanel.hidden = true;
contextPanel.append(el('h2', undefined, 'Read original context'), el('p', 'block-note', 'Choose any readable file and line, including events absent from the brief. Archives are streamed again locally. Keep this page open; the exported brief alone cannot access original files.'));
const contextControls = el('div', 'action-row');
const contextFile = el('select'); contextFile.setAttribute('aria-label', 'Source file for context');
const contextLine = el('input'); contextLine.type = 'number'; contextLine.min = '1'; contextLine.value = '1'; contextLine.setAttribute('aria-label', 'Start line');
const contextRead = button('Read 20 lines');
const contextNext = button('Next page'); contextNext.disabled = true;
const contextCancel = button('Cancel context'); contextCancel.disabled = true;
const contextDownload = button('Download context'); contextDownload.disabled = true;
contextControls.append(contextFile, contextLine, contextRead, contextNext, contextCancel, contextDownload);
const contextStatus = el('p', 'block-note', 'Each response is capped at 12 KB.'); contextStatus.setAttribute('role', 'status');
const contextContent = el('pre', 'context original-context');
contextPanel.append(contextControls, contextStatus, contextContent);

shell.append(header, controls, statusPanel, resultsPanel, contextPanel);
app.append(shell);

const dashboard = mountDashboard({ getInput: () => ({ logs: selectedFiles, evidence: result, preprocessing: running }) });
shell.append(dashboard);
onUiLanguage(next => { uiLanguagePicker.textContent = next === 'zh' ? 'English' : '中文'; uiLanguagePicker.setAttribute('aria-pressed', String(next === 'zh')); translateTree(shell); });
translateTree(shell);

function clearChildren(node: HTMLElement): void { node.replaceChildren(); }

function resetResults(message = 'Results will appear after a successful local preprocessing run.'): void {
  stopContext();
  contextPanel.hidden = true; contextContent.textContent = ''; contextFile.replaceChildren();
  contextJson = undefined; contextNextLine = undefined; briefJson = undefined;
  contextNext.disabled = true; contextDownload.disabled = true; briefButton.disabled = true;
  result = undefined;
  exportButton.disabled = true;
  resultSummary.className = 'empty-state'; resultSummary.textContent = message;
  clearChildren(metrics); clearChildren(warningArea); clearChildren(coverage); clearChildren(evidence); clearChildren(patterns);
}

function syncControls(): void {
  folderInput.disabled = running;
  filesInput.disabled = running;
  startButton.disabled = running || selectedFiles.length === 0;
  cancelButton.disabled = !running;
  exportButton.disabled = running || !result;
  briefButton.disabled = running || !briefJson;
}

function setSelection(files: FileList | null): void {
  if (!files || files.length === 0) return;
  selectedFiles = Array.from(files);
  const total = selectedFiles.reduce((sum, file) => sum + file.size, 0);
  selection.textContent = `${selectedFiles.length.toLocaleString()} file${selectedFiles.length === 1 ? '' : 's'} selected · ${bytes(total)} source bytes`;
  setUiText(runState, 'Ready for local preprocessing.');
  progressBar.classList.remove('indeterminate'); progressValue.style.width = '0%'; progressBar.setAttribute('aria-valuetext', 'No active preprocessing run'); progressMeta.textContent = 'No active file.';
  resetResults('Selection changed. Start a new local preprocessing run.');
  syncControls();
}

folderInput.addEventListener('change', () => setSelection(folderInput.files));
filesInput.addEventListener('change', () => setSelection(filesInput.files));

function renderMetric(label: string, value: string, tone?: string): void {
  const card = el('div', `metric${tone ? ` ${tone}` : ''}`);
  card.append(el('span', 'metric-label', label), el('strong', 'metric-value', value));
  metrics.append(card);
}

function elapsedText(): string {
  return startedAt === undefined ? '0s' : `${Math.max(0, (performance.now() - startedAt) / 1000).toFixed(1)}s`;
}

function severityPills(counts: Record<Severity, number>): HTMLElement {
  const group = el('div', 'severity-pills');
  for (const level of severityOrder) {
    const count = counts[level] ?? 0;
    if (count === 0 && !['error', 'warn'].includes(level)) continue;
    group.append(el('span', `severity severity-${level}`, `${level} ${count.toLocaleString()}`));
  }
  return group;
}

function statusPills(files: FileReport[]): HTMLElement {
  const counts = { processed: 0, partial: 0, skipped: 0, error: 0 };
  for (const file of files) counts[file.status] += 1;
  const group = el('div', 'severity-pills');
  for (const status of ['processed', 'partial', 'skipped', 'error'] as const) {
    group.append(el('span', `status status-${status}`, `${status} ${counts[status].toLocaleString()}`));
  }
  return group;
}

function table(headers: string[]): { wrapper: HTMLElement; body: HTMLTableSectionElement } {
  const wrapper = el('div', 'table-wrap');
  const tableNode = el('table');
  const head = el('thead'); const row = el('tr');
  for (const headerText of headers) row.append(el('th', undefined, headerText));
  head.append(row); const body = el('tbody'); tableNode.append(head, body); wrapper.append(tableNode);
  return { wrapper, body };
}

function appendCell(row: HTMLTableRowElement, value: string | HTMLElement, className?: string): void {
  const cell = el('td', className);
  if (typeof value === 'string') cell.textContent = value; else cell.append(value);
  row.append(cell);
}

function renderCoverage(files: FileReport[]): void {
  clearChildren(coverage);
  coverage.append(el('h3', undefined, 'File coverage'), el('p', 'block-note', `Showing ${Math.min(files.length, rowLimit)} of ${files.length} file reports.`));
  const { wrapper, body } = table(['File', 'Status', 'Source / entry bytes', 'Read bytes', 'Lines', 'Evidence kept / candidates', 'Reason / quality']);
  for (const report of files.slice(0, rowLimit)) {
    const row = el('tr') as HTMLTableRowElement;
    appendCell(row, report.path, 'path-cell');
    appendCell(row, el('span', `status status-${report.status}`, report.status));
    appendCell(row, bytes(report.sizeBytes));
    appendCell(row, bytes(report.expandedBytes));
    appendCell(row, report.lines.toLocaleString());
    appendCell(row, `${report.evidenceRetained ?? 0} / ${report.candidateEvents ?? 0}`);
    const details = [report.reason, report.malformedLines ? `${report.malformedLines.toLocaleString()} malformed JSON line${report.malformedLines === 1 ? '' : 's'}` : undefined, report.truncatedLines ? `${report.truncatedLines.toLocaleString()} truncated line${report.truncatedLines === 1 ? '' : 's'}` : undefined].filter((item): item is string => Boolean(item)).join('\n');
    appendCell(row, nodeText(details), 'reason-cell');
    body.append(row);
  }
  coverage.append(wrapper);
}

function evidenceText(item: Evidence): HTMLElement {
  const box = el('div', 'evidence-message');
  const before = item.contextBefore.filter(Boolean).join('\n');
  const after = item.contextAfter.filter(Boolean).join('\n');
  if (before) box.append(el('pre', 'context before', before));
  if (item.selectionReason === 'diagnostic-keyword') box.append(el('p', 'block-note', 'Keyword candidate · source severity is unknown'));
  if (item.selectionReason === 'lifecycle' || item.selectionReason === 'representative' || item.selectionReason === 'metadata') box.append(el('p', 'block-note', `Selected for ${item.selectionReason}`));
  if (item.textQuality?.length) box.append(el('p', 'block-note', `Text quality: ${item.textQuality.join(', ')}`));
  box.append(el('pre', 'context focus', item.rawLine ?? item.message));
  if (after) box.append(el('pre', 'context after', after));
  const more = button('Read wider context');
  more.addEventListener('click', () => { contextFile.value = item.sourceId; contextLine.value = String(Math.max(1, item.line - 10)); loadContext(); contextPanel.scrollIntoView({ behavior: 'smooth' }); });
  box.append(more);
  return box;
}

function renderEvidence(items: Evidence[]): void {
  clearChildren(evidence);
  const sortedItems = [...items].sort((left, right) => severityOrder.indexOf(left.severity) - severityOrder.indexOf(right.severity));
  evidence.append(el('h3', undefined, 'Representative evidence'), el('p', 'block-note', `Showing ${Math.min(sortedItems.length, rowLimit)} of ${sortedItems.length} retained events. Includes issue candidates, lifecycle events and normal activity. Additional source lines remain accessible below.`));
  if (items.length === 0) { evidence.append(el('p', 'muted', 'No issue candidates were retained. This does not establish that the robot is healthy.')); return; }
  const { wrapper, body } = table(['Severity', 'Location', 'Message and bounded context']);
  for (const item of sortedItems.slice(0, rowLimit)) {
    const row = el('tr') as HTMLTableRowElement;
    appendCell(row, el('span', `severity severity-${item.severity}`, item.severity));
    appendCell(row, `${item.file}:${item.line}${item.timestamp ? `\n${item.timestamp}` : ''}`, 'location-cell');
    appendCell(row, evidenceText(item)); body.append(row);
  }
  evidence.append(wrapper);
}

function renderPatterns(packageResult: LogPackage): void {
  clearChildren(patterns);
  patterns.append(el('h3', undefined, 'Repeated patterns'), el('p', 'block-note', `Showing ${Math.min(packageResult.patterns.length, rowLimit)} of ${packageResult.patterns.length} retained patterns.`));
  if (packageResult.patterns.length === 0) { patterns.append(el('p', 'muted', 'No repeated patterns were retained.')); return; }
  const { wrapper, body } = table(['Severity', 'Count', 'Signature', 'Example']);
  for (const pattern of packageResult.patterns.slice(0, rowLimit)) {
    const row = el('tr') as HTMLTableRowElement;
    appendCell(row, el('span', `severity severity-${pattern.severity}`, pattern.severity));
    appendCell(row, `${pattern.countComplete ? '' : '≥ '}${pattern.count.toLocaleString()}`); appendCell(row, pattern.signature, 'signature-cell');
    appendCell(row, `${pattern.example.file}:${pattern.example.line}\n${pattern.example.message}`, 'location-cell'); body.append(row);
  }
  patterns.append(wrapper);
}

function renderResult(packageResult: LogPackage): void {
  resultSummary.className = 'result-summary';
  const problematic = packageResult.files.filter((file) => file.status === 'partial' || file.status === 'error' || file.status === 'skipped').length;
  resultSummary.textContent = problematic > 0
    ? `Coverage is incomplete: ${problematic} of ${packageResult.files.length} processed entries are partial, skipped, or errored. Inspect file coverage before using this package.`
    : `Preprocessing complete: ${packageResult.files.length} entries covered locally. This is evidence extraction only, not an agent diagnosis.`;
  renderMetric('Source files selected', `${packageResult.source.selectedFiles.toLocaleString()} · ${bytes(packageResult.source.selectedBytes)}`);
  const entriesMetric = el('div', `metric${problematic ? ' metric-alert' : ''}`);
  entriesMetric.append(el('span', 'metric-label', 'Entries processed'), el('strong', 'metric-value', packageResult.files.length.toLocaleString()), statusPills(packageResult.files));
  metrics.append(entriesMetric);
  renderMetric('Lines parsed', packageResult.totals.lines.toLocaleString());
  renderMetric('Read bytes', bytes(packageResult.totals.expandedBytes));
  const compact = buildBrief(packageResult);
  briefJson = compact.json;
  renderMetric('Analysis brief', `${compact.bytes.toLocaleString()} / ${compact.maxBytes.toLocaleString()} bytes`);
  renderMetric('Archive coverage in evidence', `${packageResult.selection.sourcesRepresented} / ${packageResult.selection.sourcesWithCandidates}`);
  const severityMetric = el('div', 'metric'); severityMetric.append(el('span', 'metric-label', 'Severity totals'), severityPills(packageResult.totals.severityCounts)); metrics.append(severityMetric);
  if (packageResult.warnings.length > 0 || packageResult.totals.evidenceOmitted > 0 || packageResult.totals.patternEventsOmitted > 0) {
    const callout = el('div', 'callout'); callout.append(el('strong', undefined, 'Run warnings and limits'));
    const list = el('ul');
    for (const warning of packageResult.warnings.slice(0, rowLimit)) list.append(el('li', undefined, warning));
    if (packageResult.totals.evidenceOmitted) list.append(el('li', undefined, `${packageResult.totals.evidenceOmitted} candidate events not retained by bounded sampling/deduplication. Original lines remain available locally.`));
    if (packageResult.totals.patternEventsOmitted) list.append(el('li', undefined, `${packageResult.totals.patternEventsOmitted} pattern events omitted by configured cap.`));
    callout.append(list); warningArea.append(callout);
  }
  renderCoverage(packageResult.files); renderEvidence(packageResult.evidence); renderPatterns(packageResult);
  contextFile.replaceChildren();
  for (const file of packageResult.files.filter(file => file.retrieval === 'rescan-original')) {
    const option = el('option'); option.value = file.sourceId; option.textContent = `${file.sourceId} · ${file.path}`; contextFile.append(option);
  }
  contextPanel.hidden = contextFile.options.length === 0;
  translateTree(resultsPanel); translateTree(contextPanel);
}

function updateProgress(progress: Progress): void {
  progressBar.setAttribute('aria-valuetext', `${progress.filesCompleted.toLocaleString()} entries processed; current file ${progress.currentFile}`);
  progressMeta.textContent = `${progress.filesCompleted.toLocaleString()} entries processed · ${bytes(progress.bytesRead)} current-file bytes · ${progress.lines.toLocaleString()} lines · ${elapsedText()} elapsed · ${progress.currentFile}`;
}

function finishRun(message: string, completed = false): void {
  const elapsed = elapsedText();
  running = false; worker?.terminate(); worker = undefined; progressBar.classList.remove('indeterminate'); progressValue.style.width = completed ? '100%' : '0%'; const stateText = el('span'); setUiText(stateText, message); runState.replaceChildren(stateText, document.createTextNode(` · ${elapsed} elapsed.`)); startedAt = undefined; syncControls();
}

function start(): void {
  if (running || selectedFiles.length === 0) return;
  const currentRun = ++runId;
  resetResults('Preprocessing is running locally. Results replace any prior run.');
  running = true; startedAt = performance.now(); setUiText(runState, 'Worker is preprocessing selected sources locally.');
  progressBar.classList.add('indeterminate'); progressValue.style.width = '45%'; progressBar.setAttribute('aria-valuetext', 'Starting local preprocessing worker'); progressMeta.textContent = 'Starting worker…'; syncControls();
  try {
    const nextWorker = new Worker(new URL('./preprocess.worker.ts', import.meta.url), { type: 'module' });
    worker = nextWorker;
    nextWorker.onmessage = (event: MessageEvent<WorkerResponse>) => {
      if (currentRun !== runId) return;
      if (event.data.type === 'progress') updateProgress(event.data.progress);
      if (event.data.type === 'complete') {
        result = event.data.result; renderResult(result); updateProgress({ filesCompleted: result.files.length, filesTotal: result.source.selectedFiles, currentFile: 'Complete', bytesRead: result.totals.expandedBytes, lines: result.totals.lines });
        finishRun('Local preprocessing complete. Export is available on request.', true);
      }
      if (event.data.type === 'error') { resetResults(`Run failed: ${event.data.message}`); finishRun(`Local preprocessing failed: ${event.data.message}`); }
    };
    nextWorker.onerror = (event) => { if (currentRun === runId) { resetResults(`Worker error: ${event.message}`); finishRun(`Worker error: ${event.message}`); } };
    const request: WorkerRequest = { type: 'start', files: selectedFiles, limits: DEFAULT_LIMITS };
    nextWorker.postMessage(request);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    resetResults(`Worker could not start: ${message}`); finishRun(`Worker could not start: ${message}`);
  }
}

startButton.addEventListener('click', start);
cancelButton.addEventListener('click', () => {
  if (!running) return;
  runId += 1; worker?.terminate(); worker = undefined; running = false;
  progressBar.classList.remove('indeterminate'); progressValue.style.width = '0%'; progressBar.setAttribute('aria-valuetext', 'Run cancelled'); resetResults('Run cancelled. No partial package is available for export.'); const stateText = el('span'); setUiText(stateText, 'Run cancelled'); runState.replaceChildren(stateText, document.createTextNode(` · ${elapsedText()} elapsed.`)); startedAt = undefined; setUiText(progressMeta, 'No active file.'); syncControls();
});
exportButton.addEventListener('click', () => {
  if (!result) return;
  downloadJson(JSON.stringify(result), 'robot-log-evidence');
});

function downloadJson(json: string, name: string): void {
  const blob = new Blob([json], { type: 'application/json' });
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a');
  anchor.href = url; anchor.download = `${name}-${new Date().toISOString().replace(/[:.]/g, '-')}.json`; anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
briefButton.addEventListener('click', () => { if (briefJson) downloadJson(briefJson, 'robot-log-brief'); });
contextDownload.addEventListener('click', () => { if (contextJson) downloadJson(contextJson, 'robot-log-context'); });

function stopContext(): void {
  contextRequestId++;
  contextWorker?.terminate(); contextWorker = undefined;
  contextRead.disabled = false; contextCancel.disabled = true;
}
function loadContext(): void {
  if (!result || !contextFile.value) return;
  stopContext();
  const requestId = contextRequestId;
  const currentRun = runId;
  contextNext.disabled = true; contextDownload.disabled = true; contextJson = undefined; contextNextLine = undefined;
  contextRead.disabled = true; contextCancel.disabled = false;
  contextContent.textContent = '';
  contextStatus.textContent = 'Reading original source locally…';
  try {
    const reader = new Worker(new URL('./preprocess.worker.ts', import.meta.url), { type: 'module' });
    contextWorker = reader;
    reader.onmessage = (event: MessageEvent<WorkerResponse>) => {
      if (requestId !== contextRequestId || currentRun !== runId) return;
      const message = event.data;
      if (message.type === 'context' && message.requestId === requestId) {
        const reply = message.result;
        contextJson = JSON.stringify(reply); contextNextLine = reply.nextLine;
        contextContent.textContent = reply.lines.map(line => `${line.line}: ${line.text}`).join('\n');
        contextStatus.textContent = `${reply.path} · ${new TextEncoder().encode(contextJson).byteLength.toLocaleString()} bytes. ${reply.warning ?? ''} Source integrity is not revalidated during partial replay.`;
        stopContext(); contextNext.disabled = contextNextLine === undefined; contextDownload.disabled = false;
      } else if (message.type === 'error' && message.requestId === requestId) {
        stopContext(); contextStatus.textContent = `Context could not be read: ${message.message}`;
      }
    };
    reader.onerror = event => { if (requestId === contextRequestId) { stopContext(); contextStatus.textContent = `Context reader error: ${event.message}`; } };
    reader.postMessage({ type: 'context', requestId, files: selectedFiles, limits: DEFAULT_LIMITS, query: { sourceId: contextFile.value, startLine: Math.max(1, Math.floor(Number(contextLine.value)) || 1), maxLines: 20, maxBytes: 12000 } } satisfies WorkerRequest);
  } catch (error) { stopContext(); contextStatus.textContent = String(error); }
}
contextRead.addEventListener('click', loadContext);
contextNext.addEventListener('click', () => { if (contextNextLine !== undefined) { contextLine.value = String(contextNextLine); loadContext(); } });
contextCancel.addEventListener('click', () => { stopContext(); contextStatus.textContent = 'Context request cancelled.'; });
contextFile.addEventListener('change', () => {
  stopContext(); contextJson = undefined; contextNextLine = undefined; contextNext.disabled = true; contextDownload.disabled = true; contextContent.textContent = ''; contextLine.value = '1'; contextStatus.textContent = 'Select a starting line and read up to 12 KB.';
});

syncControls();
