import type { LogPackage } from './types';
import { onUiLanguage, setUiText, translate, translateTree, uiLanguage } from './i18n';

type Job = Record<string, unknown>;
type Version = Record<string, unknown>;
type Input = { logs: File[]; evidence?: LogPackage; preprocessing: boolean };
export type DashboardOptions = { getInput: () => Input };

const API = '/api';
const MAX_FILES = 25, MAX_BYTES = 250 * 1024 * 1024;
const key = (id: string) => `robot-log-claim-${id}`;
const text = (v: unknown, fallback = '—') => typeof v === 'string' && v.trim() ? v : v == null ? fallback : String(v);
const node = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, value?: string): HTMLElementTagNameMap[K] => { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; };
const button = (value: string, cls = 'button') => { const n = node('button', cls, value); n.type = 'button'; return n; };
const id = (job: Job) => text(job.id ?? job.job_id);
const state = (job: Job) => text(job.status ?? job.state, 'queued').toLowerCase();
const failure = (e: unknown) => e instanceof Error ? e.message : String(e);
const totalBytes = (files: File[]) => files.reduce((sum, file) => sum + file.size, 0);
const size = (n: number) => n < 1048576 ? `${Math.ceil(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;

async function api(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(`${API}${path}`, init);
  if (!response.ok) throw Object.assign(new Error((await response.text()) || `${response.status} ${response.statusText}`), { status: response.status });
  return response.status === 204 ? undefined : response.json();
}

/** A shared job UI. It only reads current browser preprocessing state. */
export function mountDashboard({ getInput }: DashboardOptions): HTMLElement {
  const panel = node('section', 'panel dashboard');
  const header = node('div', 'section-heading'), left = node('div');
  left.append(node('p', 'section-kicker', 'SHARED'), node('h2', undefined, '4. Shared analysis queue'));
  const backend = node('p', 'local-note', 'Checking backend…'); header.append(left, backend);
  const description = node('textarea', 'analysis-description') as HTMLTextAreaElement;
  description.rows = 4; description.maxLength = 8000; description.placeholder = 'Describe the analysis question, symptoms, and constraints.';
  const language = node('select', 'analysis-language') as HTMLSelectElement;
  for (const [v, label] of [['en', 'English'], ['zh', '中文']]) { const option = node('option'); option.value = v; option.textContent = label; language.append(option); }
  const attachmentInput = node('input') as HTMLInputElement; attachmentInput.type = 'file'; attachmentInput.multiple = true; attachmentInput.accept = 'image/*,application/pdf'; attachmentInput.id = 'analysis-attachments';
  const attach = node('label', 'picker-button secondary', 'Attach images or PDFs'); attach.htmlFor = attachmentInput.id; attach.append(attachmentInput);
  const attachmentStatus = node('p', 'format-note', 'Browser feedback limit: 25 attachments / 250 MB. Server validation still applies.');
  const privacy = node('input') as HTMLInputElement; privacy.type = 'checkbox'; privacy.id = 'shared-upload-confirmation';
  const privacyLabel = node('label', 'privacy-confirmation', 'I understand that creating shared analysis uploads selected original files, full evidence, and attachments to the shared backend.'); privacyLabel.htmlFor = privacy.id; privacyLabel.prepend(privacy);
  const submit = button('Create shared analysis', 'button primary');
  const status = node('p', 'dashboard-status', 'Logs are preprocessed locally first. Attachments alone are also allowed.'); status.setAttribute('role', 'status');
  const form = node('div', 'analysis-form'); form.append(node('label', 'field-label', 'Analysis request'), description, node('label', 'field-label', 'Output language'), language, attach, attachmentStatus, privacyLabel, submit, status);
  const budget = node('p', 'budget-status', 'Daily budget: checking…'); const readiness = node('p', 'model-unavailable', 'Analysis worker/model unavailable: the queue API is available, but real analysis requires a separately configured worker and model.'); const jobs = node('div', 'job-list'); const older = button('Load older jobs'); older.hidden = true;
  panel.append(header, form, budget, readiness, node('h3', undefined, 'All analysis jobs'), jobs, older);

  let attachments: File[] = [], busy = false, budgetOpen = true, stopped = false, timer: number | undefined, outputLanguageChanged = false;
  let jobList: Job[] = [], nextCursor: string | null = null, loadingOlder = false, budgetValues: { spent: number; used: number; limit: number } | undefined;
  const versions = new Map<string, Version[]>(), eventAfter = new Map<string, number>(), activityCache = new Map<string, string[]>(), reports = new Map<string, string>(), reviewDrafts = new Map<string, { success: string; why: string; procedure: string }>();
  const placeholders = () => { description.placeholder = uiLanguage() === 'zh' ? '描述分析问题、症状和约束。' : 'Describe the analysis question, symptoms, and constraints.'; };
  const attachmentTooLarge = () => attachments.length > MAX_FILES || totalBytes(attachments) > MAX_BYTES;
  const sync = () => {
    const input = getInput();
    submit.disabled = busy || attachmentTooLarge() || !privacy.checked || input.preprocessing || (input.logs.length > 0 && !input.evidence) || (!input.logs.length && !attachments.length);
    if (input.preprocessing) setUiText(status, 'Wait for selected logs to finish local preprocessing.');
    else if (input.logs.length && !input.evidence) setUiText(status, 'Selected logs require a fresh local preprocessing run.');
    else if (!input.logs.length && !attachments.length) setUiText(status, 'Choose logs or an image/PDF attachment.');
  };
  const renderBudget = () => { if (!budgetValues) return; const { spent, used, limit } = budgetValues; budget.textContent = uiLanguage() === 'zh' ? `每日分析预算：已花费 $${spent.toFixed(2)} · 已准入 $${used.toFixed(2)} / $${limit.toFixed(2)}${budgetOpen ? '' : ' · 已停止准入'}` : `Daily analysis budget: $${spent.toFixed(2)} spent · $${used.toFixed(2)} admitted / $${limit.toFixed(2)}${budgetOpen ? '' : ' · admission closed'}`; };
  const schedule = () => { if (!stopped && timer === undefined) timer = window.setTimeout(() => { timer = undefined; void refresh(); }, 2000); };
  const versionsFor = async (jobId: string) => {
    try { const result = await api(`/jobs/${encodeURIComponent(jobId)}/versions`) as unknown; versions.set(jobId, Array.isArray(result) ? result as Version[] : (result as { versions?: Version[] }).versions ?? []); } catch { /* endpoint failure does not hide jobs */ }
  };
  const listJobPage = async (cursor: string | null): Promise<{ items: Job[]; next: string | null }> => {
    const reply = await api(`/jobs?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
    if (Array.isArray(reply)) return { items: reply as Job[], next: null };
    const page = reply as { items?: Job[]; jobs?: Job[]; next_cursor?: string | null };
    return { items: page.items ?? page.jobs ?? [], next: page.next_cursor ?? null };
  };
  const eventText = (event: unknown): string | null => {
    if (!event || typeof event !== 'object') return null; const data = event as Record<string, unknown>;
    const kind = typeof data.kind === 'string' ? data.kind.slice(0, 20) : 'activity'; const agent = typeof data.agent === 'string' ? data.agent.slice(0, 80) : 'worker'; const message = typeof data.message === 'string' ? data.message.slice(0, 2000) : '';
    return message ? `${agent} · ${kind}: ${message}` : null;
  };
  const activityFor = async (jobId: string, target: HTMLElement) => {
    const after = eventAfter.get(jobId) ?? 0;
    try { const result = await api(`/jobs/${encodeURIComponent(jobId)}/events?after=${Number.isFinite(after) ? after : 0}`) as { events?: unknown[]; next?: number; after?: number } | unknown[];
      const events = Array.isArray(result) ? result : result.events ?? []; const safe = events.map(eventText).filter((x): x is string => Boolean(x));
      if (safe.length) { const recent = [...(activityCache.get(jobId) ?? []), ...safe].slice(-3); activityCache.set(jobId, recent); target.textContent = recent.join('\n'); }
      if (!Array.isArray(result)) eventAfter.set(jobId, Number(result.next ?? result.after ?? after + events.length));
    } catch { target.textContent = 'Agent activity unavailable.'; }
  };
  const render = (force = false) => {
    if (!force && document.activeElement instanceof HTMLElement && document.activeElement.closest('.review-form')) return;
    jobs.replaceChildren();
    if (!jobList.length) { jobs.append(node('p', 'muted', 'No shared jobs yet.')); translateTree(jobs); return; }
    for (const job of jobList) {
      const jobId = id(job), current = state(job), cancelling = current === 'running' && Boolean(job.cancel_requested), visibleState = cancelling ? 'stopping' : current === 'draft' ? 'uploading' : current, card = node('article', `job-card state-${visibleState}`), top = node('div', 'job-top');
      const jobTitle = node('strong', undefined, `#${jobId} · `), stateLabel = node('span'); setUiText(stateLabel, visibleState); jobTitle.append(stateLabel); top.append(jobTitle, node('span', 'muted', text(job.created_at ?? job.createdAt, ''))); card.append(top);
      if (job.run_deadline) card.append(node('p', 'muted', `Timeout: ${text(job.run_deadline)}`));
      if (job.description) { const descriptionNode = node('p', 'job-description', text(job.description)); descriptionNode.dataset.i18nSkip = ''; card.append(descriptionNode); }
      const activity = node('p', 'agent-activity', (activityCache.get(jobId) ?? []).join('\n') || 'Loading sanitized agent activity…'); activity.dataset.i18nSkip = ''; card.append(activity); void activityFor(jobId, activity);
      const report = reports.get(jobId) ?? job.final_report ?? job.report ?? job.result;
      if (typeof report === 'string') { const final = node('pre', 'final-report'); final.textContent = report; card.append(node('h4', undefined, 'Final report'), final); }
      else if (['completed', 'failed', 'cancelled'].includes(current)) { const loadReport = button('Load final report'); loadReport.addEventListener('click', async () => { loadReport.disabled = true; try { const detail = await api(`/jobs/${encodeURIComponent(jobId)}`) as Job; const final = detail.report ?? detail.final_report ?? detail.result; if (typeof final === 'string') { reports.set(jobId, final); render(); } else loadReport.textContent = 'No final report available'; } catch (error) { loadReport.disabled = false; status.textContent = `Could not load report: ${failure(error)}`; } }); card.append(loadReport); }
      const actions = node('div', 'action-row'); const hardStop = button('Hard stop', 'button danger');
      hardStop.disabled = cancelling || !['draft', 'queued', 'running', 'uploading'].includes(current);
      hardStop.addEventListener('click', async () => { if (!window.confirm(`Hard stop job #${jobId}? This requests cancellation of the entire agent tree.`)) return; try { await api(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }); status.textContent = `Stop requested for job #${jobId}; it is stopping until the worker acknowledges cancellation.`; await refresh(); } catch (error) { status.textContent = `Hard stop ${((error as { status?: number }).status === 409) ? 'conflict' : 'failed'}: ${failure(error)}`; } });
      const token = localStorage.getItem(key(jobId)); const claim = button(token ? 'Review claimed' : 'Claim review'); claim.disabled = Boolean(token) || current !== 'completed';
      claim.addEventListener('click', async () => { const name = window.prompt(translate('Reviewer name')); if (!name?.trim()) return; try { const reply = await api(`/jobs/${encodeURIComponent(jobId)}/claim`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }) }) as { claim_token?: string }; if (!reply.claim_token) throw new Error('Missing claim token.'); localStorage.setItem(key(jobId), reply.claim_token); await refresh(); } catch (error) { status.textContent = `Claim ${((error as { status?: number }).status === 409) ? 'conflict' : 'failed'}: ${failure(error)}`; } });
      actions.append(hardStop, claim); card.append(actions);
      if (token) {
        const review = node('div', 'review-form'), success = node('select') as HTMLSelectElement, why = node('textarea') as HTMLTextAreaElement, procedure = node('textarea') as HTMLTextAreaElement;
        for (const [value, label] of [['true', 'Success'], ['false', 'Not successful']]) { const option = node('option'); option.value = value; option.textContent = label; success.append(option); }
        const draft = reviewDrafts.get(jobId) ?? { success: 'true', why: '', procedure: '' }; success.value = draft.success; why.value = draft.why; procedure.value = draft.procedure;
        why.dataset.placeholderKey = 'why'; procedure.dataset.placeholderKey = 'procedure'; why.placeholder = uiLanguage() === 'zh' ? '此审核结论为何正确？' : 'Why is this review outcome correct?'; procedure.placeholder = uiLanguage() === 'zh' ? '修改后的调试步骤' : 'Edited debug procedure';
        const saveDraft = () => reviewDrafts.set(jobId, { success: success.value, why: why.value, procedure: procedure.value }); success.addEventListener('change', saveDraft); why.addEventListener('input', saveDraft); procedure.addEventListener('input', saveDraft);
        const release = button('Save immutable review & release'); release.addEventListener('click', async () => { if (!why.value.trim() || !procedure.value.trim()) { status.textContent = 'Review needs success, why, and edited debug procedure.'; return; } try { await api(`/jobs/${encodeURIComponent(jobId)}/versions`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ success: success.value === 'true', note: why.value.trim(), procedure: procedure.value.trim() }) }); localStorage.removeItem(key(jobId)); reviewDrafts.delete(jobId); await refresh(true); } catch (error) { status.textContent = `Review ${((error as { status?: number }).status === 409) ? 'conflict' : 'failed'}: ${failure(error)}`; } });
        const forget = button('Forget stale claim'); forget.addEventListener('click', async () => { try { await api(`/jobs/${encodeURIComponent(jobId)}/claim`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } }); } catch { /* Expired claims cannot be released, but the local token can be discarded. */ } localStorage.removeItem(key(jobId)); reviewDrafts.delete(jobId); await refresh(); });
        review.append(success, why, procedure, release, forget); card.append(review);
      }
      const allVersions = versions.get(jobId) ?? [];
      if (allVersions.length) { const list = node('ol', 'version-list'); for (const version of allVersions) { const item = node('li'); const reviewer = node('span', undefined, text(version.reviewer_name, 'anonymous')); reviewer.dataset.i18nSkip = ''; const why = node('p'); const note = node('span', undefined, text(version.note ?? version.why)); note.dataset.i18nSkip = ''; why.append(node('span', undefined, 'Why: '), note); const procedure = node('p'); const procedureText = node('span', undefined, text(version.procedure)); procedureText.dataset.i18nSkip = ''; procedure.append(node('span', undefined, 'Edited debug procedure: '), procedureText); const versionTitle = node('strong', undefined, `v${text(version.id ?? version.version ?? version.number, '?')} · `), outcome = node('span'); setUiText(outcome, version.success ? 'Success' : 'Not successful'); versionTitle.append(outcome, document.createTextNode(' · ')); item.append(versionTitle, reviewer, why, procedure); list.append(item); } card.append(node('h4', undefined, 'Immutable review versions'), list); }
      translateTree(card); jobs.append(card);
    }
  };
  const refresh = async (forceRender = false) => {
    if (stopped) return;
    try {
      const [page, budgetReply] = await Promise.all([listJobPage(null), api('/budget')]);
      const retained = new Map(jobList.map(job => [id(job), job])); for (const job of page.items) retained.set(id(job), job);
      jobList = [...retained.values()].sort((a, b) => text(b.created_at ?? b.createdAt, '').localeCompare(text(a.created_at ?? a.createdAt, ''))); nextCursor = page.next; older.hidden = !nextCursor;
      const b = budgetReply as Record<string, unknown>, spent = Number(b.spent_usd ?? b.spent ?? b.used ?? 0), limit = Number(b.daily_limit_usd ?? b.limit ?? b.daily_limit ?? 10), used = Number(b.admission_used_usd ?? spent), estimate = Number(b.estimated_per_job_usd ?? 0), remaining = Number(b.remaining ?? limit - used);
      budgetOpen = !Number.isFinite(remaining) || remaining >= estimate; budgetValues = { spent, used, limit }; renderBudget();
      backend.className = 'local-note'; setUiText(backend, 'Backend connected'); await Promise.all(jobList.map(job => versionsFor(id(job)))); render(forceRender);
    } catch { backend.className = 'backend-offline'; setUiText(backend, 'Backend offline — local preprocessing still works.'); budget.textContent = uiLanguage() === 'zh' ? '后端离线，无法读取每日预算。' : 'Daily budget unavailable while backend is offline.'; }
    sync(); schedule();
  };
  attachmentInput.addEventListener('change', () => { attachments = Array.from(attachmentInput.files ?? []); const bad = attachments.length > MAX_FILES || totalBytes(attachments) > MAX_BYTES; attachmentStatus.textContent = bad ? (uiLanguage() === 'zh' ? `超出浏览器限制：${attachments.length} 个文件 / ${size(totalBytes(attachments))}。提交前请减少；服务器仍会验证。` : `Client limit exceeded: ${attachments.length} files / ${size(totalBytes(attachments))}. Reduce before submit; server validation still applies.`) : attachments.length ? (uiLanguage() === 'zh' ? `${attachments.length} 个附件，${size(totalBytes(attachments))}。` : `${attachments.length} attachment(s), ${size(totalBytes(attachments))}.`) : translate('Browser feedback limit: 25 attachments / 250 MB. Server validation still applies.'); sync(); });
  privacy.addEventListener('change', sync);
  older.addEventListener('click', async () => { if (!nextCursor || loadingOlder) return; loadingOlder = true; older.disabled = true; try { const page = await listJobPage(nextCursor); const known = new Map(jobList.map(job => [id(job), job])); for (const job of page.items) known.set(id(job), job); jobList = [...known.values()].sort((a, b) => text(b.created_at ?? b.createdAt, '').localeCompare(text(a.created_at ?? a.createdAt, ''))); nextCursor = page.next; older.hidden = !nextCursor; await Promise.all(page.items.map(job => versionsFor(id(job)))); render(); } catch (error) { status.textContent = `Could not load older jobs: ${failure(error)}`; } finally { loadingOlder = false; older.disabled = false; } });
  language.value = uiLanguage();
  language.addEventListener('change', () => { outputLanguageChanged = true; });
  onUiLanguage(next => { if (!outputLanguageChanged) language.value = next; placeholders(); for (const field of panel.querySelectorAll<HTMLTextAreaElement>('[data-placeholder-key]')) field.placeholder = field.dataset.placeholderKey === 'why' ? (next === 'zh' ? '此审核结论为何正确？' : 'Why is this review outcome correct?') : (next === 'zh' ? '修改后的调试步骤' : 'Edited debug procedure'); renderBudget(); translateTree(panel); });
  submit.addEventListener('click', async () => {
    const input = getInput(); if (!description.value.trim()) { status.textContent = 'Add an analysis description.'; return; } if (!privacy.checked) { status.textContent = 'Confirm the shared upload before creating analysis.'; return; } if (submit.disabled) { sync(); return; }
    busy = true; sync(); status.textContent = 'Creating shared job…';
    try {
      const created = await api('/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ description: description.value.trim(), language: language.value, evidence: input.evidence }) }) as { job?: Job; upload_token?: string };
      const job = created.job, jobId = job && id(job); if (!jobId || !created.upload_token) throw new Error('Missing job or upload token.');
      for (const file of input.logs) { const name = file.webkitRelativePath || file.name; await api(`/jobs/${encodeURIComponent(jobId)}/files?name=${encodeURIComponent(name)}`, { method: 'PUT', headers: { Authorization: `Bearer ${created.upload_token}`, 'Content-Type': file.type || 'application/octet-stream' }, body: file }); }
      for (const file of attachments) await api(`/jobs/${encodeURIComponent(jobId)}/files?name=${encodeURIComponent(file.name)}`, { method: 'PUT', headers: { Authorization: `Bearer ${created.upload_token}`, 'Content-Type': file.type || 'application/octet-stream' }, body: file });
      await api(`/jobs/${encodeURIComponent(jobId)}/submit`, { method: 'POST', headers: { Authorization: `Bearer ${created.upload_token}` } }); description.value = ''; attachments = []; attachmentInput.value = ''; status.textContent = `Shared job #${jobId} submitted.`; await refresh();
    } catch (error) { status.textContent = `Submission failed: ${failure(error)}`; } finally { busy = false; sync(); }
  });
  placeholders(); translateTree(panel); void refresh(); sync(); return panel;
}
