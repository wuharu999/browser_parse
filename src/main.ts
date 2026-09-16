import './style.css';
import { observedAgents, subagentBadge, type SubagentState, type SubagentSummary } from './subagents';
import { DEFAULT_LIMITS, type LogPackage, type WorkerResponse } from './types';
import { parseReport } from './report';
import { availability, type Budget } from './budget';
import { uploadFile, protectUpload, type UploadProgress, type WakeState } from './upload';
import { onUiLanguage, setUiLanguage, t, uiLanguage } from './i18n';

type Job = { codex_started?: boolean; subagents?: SubagentSummary[]; id: string; description: string; status: string; created_at: string; cancel_requested: boolean; report?: string; resource_plan?: { profile: string; cpu_milli: number; memory_mb: number; disk_mb: number } | null; review_claim?: { name: string; expires_at: string } | null; sanitized_description?: string };
type Version = { id: number; reviewer_name: string; success: boolean; note: string; procedure: string; created_at: string };
type Event = { subagent?: SubagentState | null; seq: number; agent: string; kind: string; message: string; created_at?: string };
type Draft = { name: string; procedure: string; note: string; verdict: string; token?: string; expires_at?: string };
const terminal = (job: Job) => ['completed', 'failed', 'cancelled'].includes(job.status);
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text?: string): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag); node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = (text: string, cls: string, action: () => void) => { const node = el('button', cls, text); node.type = 'button'; node.addEventListener('click', action); return node; };
const field = (label: string, input: HTMLElement) => { const node = el('label', 'field'); node.append(el('span', 'field-label', label), input); return node; };
const size = (bytes: number) => bytes < 1e6 ? `${Math.ceil(bytes / 1000)} KB` : `${(bytes / 1e6).toFixed(1)} MB`;
const date = (value: string) => new Date(value).toLocaleString(uiLanguage() === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const title = (job: Job) => job.description.replace(/^\[DEMO\]\s*/, '').split('\n')[0].slice(0, 100);
const claimKey = (id: string) => `robot-log-claim-${id}`;
function savedClaim(job: Job): { token: string; name: string; expires_at: string } | undefined {
  try { const value = JSON.parse(localStorage.getItem(claimKey(job.id)) ?? 'null'); if (value && typeof value.token === 'string' && value.name === job.review_claim?.name && value.expires_at === job.review_claim?.expires_at && Date.parse(value.expires_at) > Date.now()) return value; } catch { /* Legacy or expired claim must not impersonate another reviewer. */ }
  localStorage.removeItem(claimKey(job.id)); return undefined;
}
const statusText = (job: Job) => job.status === 'running' && job.cancel_requested ? t('Stopping', '停止中') : ({ draft: t('Uploading', '上传中'), queued: t('Queued', '排队中'), running: t('Analyzing', '分析中'), completed: t('Completed', '已完成'), failed: t('Failed', '失败'), cancelled: t('Cancelled', '已取消') }[job.status] ?? job.status);
const statusBadge = (job: Job) => el('span', `status-badge ${job.status}`, statusText(job));

let jobs: Job[] = [], active: Job[] = [], cursor: string | null = null, selected: string | null = null, historyInitialized = false;
let online = false, busy = false, files: File[] = [], description = '', outputLanguage = uiLanguage(), outputChosen = false;
let budget: Budget | null = null;
let transfer: (UploadProgress & { index: number; count: number; name: string }) | undefined;
let wakeState: WakeState = 'requesting';
let submitMessage = '', controller: AbortController | undefined, selectedJob: Job | undefined, selectedVersions: Version[] = [];
let selectedEvents: Event[] = [], detailSignature = '', activitySignature = '', detailSequence = 0, detailNeedsRender = false, refreshing = false;
const drafts = new Map<string, Draft>(), events = new Map<string, Event[]>();
const sessionViews = new Map<string, { open: boolean; top: number; following: boolean }>();
const processDrawers = new Map<string, boolean>();
const retainedAgentEvents = new Map<string, Event[]>();
const pausedSessions = new Set<string>();
const isHeartbeat = (event: Event): boolean => event.kind === 'heartbeat' || event.message.includes('Sandbox execution remains active');
const findJob = (id: string): Job | undefined => (selectedJob?.id === id ? selectedJob : active.find(j => j.id === id) ?? jobs.find(j => j.id === id));
const app = document.querySelector<HTMLDivElement>('#app')!;
let history: HTMLElement, content: HTMLElement, activity: HTMLElement, connection: HTMLElement, banner: HTMLElement, activityCount: HTMLElement, usage: HTMLElement, uploadPanel: HTMLElement;

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  if (!response.ok) {
    let message = `${response.status}`;
    try { const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : message; } catch { /* status is enough */ }
    throw new Error(message);
  }
  return response.status === 204 ? undefined as T : response.json();
}
const json = (body: unknown, token?: string): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body) });
function notice(message: string, error = false): void { banner.textContent = message; banner.hidden = !message; banner.className = `notice ${error ? 'error' : ''}`; }

function buildShell(): void {
  document.documentElement.lang = uiLanguage(); app.replaceChildren();
  const shell = el('div', 'workspace'), sidebar = el('aside', 'sidebar');
  const brand = el('div', 'brand'); brand.append(el('span', 'brand-mark', 'R'), el('span', '', t('Robot Logs', '机器人日志')));
  sidebar.append(brand, button(t('+  New analysis', '+  新建分析'), 'button primary new-analysis', showNew), el('div', 'sidebar-label', t('ANALYSIS HISTORY', '分析历史')));
  history = el('nav', 'history-list'); history.setAttribute('aria-label', t('Analysis history', '分析历史')); sidebar.append(history);
  connection = el('div', 'connection'); sidebar.append(connection);
  const main = el('main', 'main'), topbar = el('header', 'topbar');
  topbar.append(el('span', 'workspace-title', t('Shared workspace', '共享工作台')));
  const tools = el('div', 'top-actions'); activityCount = el('span', 'activity-count');
  const languageButton = button(uiLanguage() === 'en' ? '中文' : 'English', 'button text-button language-toggle', () => setUiLanguage(uiLanguage() === 'en' ? 'zh' : 'en'));
  languageButton.setAttribute('aria-label', 'Switch interface language / 切换界面语言'); tools.append(activityCount, languageButton); topbar.append(tools);
  banner = el('div', 'notice'); banner.hidden = true; banner.setAttribute('role', 'status');
  activity = el('section', 'live-panel'); activity.setAttribute('aria-label', t('Shared processes', '共享进程'));
  usage = el('section', 'usage-panel'); usage.setAttribute('aria-label', t('Daily analysis allowance', '每日分析额度'));
  uploadPanel = el('section', 'upload-panel'); uploadPanel.setAttribute('aria-label', t('Upload status', '上传状态'));
  content = el('div', 'content'); main.append(topbar, banner, usage, activity, uploadPanel, content); shell.append(sidebar, main); app.append(shell);
  renderHistory(); renderActivity(); updateConnection(); renderBudget(); renderUpload();
  if (selected && selectedJob) renderResult(); else renderNew();
}
function submissionLabel(): string {
  const state = availability(budget).state;
  return busy ? t('Working…', '处理中…') : state === 'unknown' ? t('Checking availability…', '正在检查额度…') : state === 'queue_full' ? t('Queue full', '队列已满') : state === 'budget_wait' ? t('Daily limit reached (20/20)', '今日额度已满 (20/20)') : state === 'available' ? t('Analyze incident  →', '开始分析  →') : t('Queue analysis  →', '加入分析队列  →');
}
function renderBudget(): void {
  const state = availability(budget);
  usage.replaceChildren();
  if (!budget || state.state === 'unknown') {
    usage.append(el('p', 'usage-status', t('Daily allowance unavailable. Checking again; your upload draft is preserved.', '暂时无法读取每日额度，正在重试；上传草稿已保留。')));
  } else {
    const limitShots = budget.daily_limit_shots ?? Math.round(budget.daily_limit_usd ?? 20);
    const usedShots = budget.used_today ?? Math.round(budget.admission_used_usd ?? 0);
    const completedShots = budget.completed_today ?? Math.round(budget.spent_usd ?? 0);
    const runningShots = budget.running ?? 0;
    const remainingShots = Math.max(0, limitShots - usedShots);

    const top = el('div', 'usage-heading');
    top.append(
      el('strong', '', t("Today's analysis quota", '今日分析额度')),
      el('span', '', `${usedShots} / ${limitShots} ${t('shots', '次')}`)
    );
    const bar = el('div', 'usage-bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', t('Completed and running analyses today', '今日已完成与运行中分析任务'));
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', String(limitShots));
    bar.setAttribute('aria-valuenow', String(Math.min(limitShots, usedShots)));
    bar.setAttribute('aria-valuetext', `${usedShots} / ${limitShots} ${t('shots', '次')}`);

    const settled = el('span', 'usage-settled'), reserved = el('span', 'usage-reserved');
    settled.style.width = `${state.settledPercent}%`;
    reserved.style.width = `${state.reservedPercent}%`;
    bar.append(settled, reserved);

    const messages: Record<string, string> = {
      available: t(`You can submit a new analysis (remaining: ${remainingShots}/${limitShots}). It starts when a worker is ready.`, `可以提交新分析（今日剩余 ${remainingShots}/${limitShots} 次）；工作器就绪后开始。`),
      budget_wait: t('Daily limit of 20 analyses reached. Resets at 00:00 (Asia/Shanghai).', '今日 20 次分析额度已全部用完，将于次日 00:00（Asia/Shanghai）重置。'),
      capacity_wait: t('Workers are at capacity. New analyses will queue.', '运行名额已满，新分析将排队。'),
      queue_wait: t('You can submit; queued analyses are ahead of the new task.', '可以提交新分析；已有排队任务将先处理。'),
      queue_full: t('Queue full. Wait for a pending slot before submitting a new analysis.', '队列已满。请等待出现空位后再提交新分析。'),
    };

    usage.append(
      top,
      bar,
      el('p', 'usage-breakdown', t(
        `Completed: ${completedShots} · Running: ${runningShots} · Remaining: ${remainingShots}`,
        `已完成：${completedShots} 次 · 运行中：${runningShots} 次 · 剩余：${remainingShots} 次`
      )),
      el('p', 'usage-status', messages[state.state] || messages.available),
      el('p', 'usage-note', t(
        `Hard limit: ${limitShots} analyses per day · ${budget.pending}/${budget.max_pending} pending slots used. Daily reset: ${budget.resets_at.slice(0, 10)} 00:00 (Asia/Shanghai).`,
        `每日上限：${limitShots} 次分析任务 · 待处理名额 ${budget.pending}/${budget.max_pending}。每日重置：${budget.resets_at.slice(0, 10)} 00:00（Asia/Shanghai）。`
      ))
    );
    if (budget.resource_envelope) {
      const envelope = budget.resource_envelope;
      usage.append(el('p', 'usage-note', t(
        `Maximum per job: ${envelope.cpu_milli / 1000} CPU · ${envelope.memory_mb / 1024} GiB RAM · ${envelope.disk_mb / 1024} GiB disk. Actual worker availability is checked when a worker claims the job.`,
        `单个任务上限：CPU ${envelope.cpu_milli / 1000} 核 · 内存 ${envelope.memory_mb / 1024} GiB · 磁盘 ${envelope.disk_mb / 1024} GiB。实际工作器可用性会在工作器领取任务时确认。`
      )));
    }
  }
  const start = content.querySelector<HTMLButtonElement>('.start-analysis');
  if (start) { start.textContent = submissionLabel(); start.disabled = busy || !state.canSubmit; }
}
function updateConnection(): void {
  connection.replaceChildren(el('span', `connection-dot ${online ? 'online' : ''}`), el('span', '', online ? t('Shared with everyone', '所有人共享') : t('Connecting to server…', '正在连接服务器…')));
}
function renderHistory(): void {
  const savedHistoryScroll = history?.scrollTop ?? 0;
  history.replaceChildren();
  if (!jobs.length) history.append(el('p', 'sidebar-empty', t('Your analyses will appear here.', '分析记录将显示在这里。')));
  for (const job of jobs) {
    const item = button('', `history-item ${selected === job.id ? 'selected' : ''}`, () => { void selectJob(job.id); });
    item.setAttribute('aria-current', selected === job.id ? 'page' : 'false');
    item.append(el('span', 'history-title', title(job)), el('span', 'history-meta', `${date(job.created_at)} · ${statusText(job)}`));
    if (job.id.startsWith('demo-')) item.append(el('span', 'demo-tag', t('DEMO', '示例')));
    history.append(item);
  }
  if (cursor) history.append(button(t('Load older analyses', '加载更早分析'), 'button text-button', () => { void loadOlder(); }));
  if (savedHistoryScroll) history.scrollTop = savedHistoryScroll;
}
async function loadOlder(): Promise<void> {
  try { const page = await api<{ items: Job[]; next_cursor: string | null }>(`/jobs?limit=50&cursor=${encodeURIComponent(cursor ?? '')}`); mergeJobs(page.items); cursor = page.next_cursor; renderHistory(); }
  catch (error) { notice(`${t('Could not load history', '无法加载历史')}：${error}`, true); }
}
function mergeJobs(incoming: Job[]): void {
  const all = new Map(jobs.map(job => [job.id, job])); for (const job of incoming) all.set(job.id, job);
  jobs = [...all.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
}
function renderActivity(): void {
  for (const panel of activity.querySelectorAll<HTMLDetailsElement>('.session-panel')) {
    const transcript = panel.querySelector<HTMLElement>('.session-transcript');
    if (transcript) sessionViews.set(panel.dataset.job!, { open: panel.open, top: transcript.scrollTop, following: transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 30 });
  }
  const running = active.filter(job => job.status === 'running').length, queued = active.filter(job => job.status === 'queued').length;
  activityCount.textContent = t(`${running} running · ${queued} queued`, `${running} 个运行中 · ${queued} 个排队中`);

  const signature = JSON.stringify([
    online,
    active.map(j => [j.id, j.status, j.cancel_requested, j.resource_plan, j.codex_started, j.subagents]),
    active.map(j => (events.get(j.id) ?? []).length),
    active.map(j => (events.get(j.id) ?? []).at(-1)?.seq),
    active.map(j => (events.get(j.id) ?? []).at(-1)?.message),
  ]);
  if (signature === activitySignature) return;
  activitySignature = signature;

  const existingList = activity.querySelector<HTMLElement>('.live-list');
  const savedLiveScroll = existingList ? existingList.scrollTop : 0;
  const savedWindowScroll = window.scrollY;
  const savedDrawers = new Map<string, number>();
  for (const drawer of activity.querySelectorAll<HTMLDetailsElement>('.process-output-drawer')) {
    const body = drawer.querySelector<HTMLElement>('.process-drawer-body');
    if (body && drawer.dataset.drawerKey) savedDrawers.set(drawer.dataset.drawerKey, body.scrollTop);
  }

  activity.replaceChildren();
  if (!active.length) { activity.append(el('p', 'quiet-state', online ? t('●  No analyses running right now', '●  当前没有正在运行的分析') : t('Checking shared processes…', '正在检查共享进程…'))); return; }
  const heading = el('div', 'live-heading'); heading.append(el('h2', '', t('Running & queued', '运行与排队')), el('span', 'muted', t('Visible to everyone', '所有人可见'))); activity.append(heading);
  const list = el('div', 'live-list');
  for (const job of active) {
    const card = el('article', 'process-card'), top = el('div', 'process-top');
    top.append(button(title(job), 'process-title', () => { void selectJob(job.id); }), statusBadge(job));
    card.append(top, sessionPanel(job.id, events.get(job.id) ?? []));
    const stop = button(t('Hard stop', '强制停止'), 'button danger compact', () => { void stopJob(job); }); stop.disabled = job.cancel_requested;
    card.append(stop); list.append(card);
  }
  activity.append(list);
  if (savedLiveScroll) list.scrollTop = savedLiveScroll;
  for (const panel of activity.querySelectorAll<HTMLDetailsElement>('.session-panel')) {
    const transcript = panel.querySelector<HTMLElement>('.session-transcript')!, state = sessionViews.get(panel.dataset.job!);
    transcript.scrollTop = !state || state.following ? transcript.scrollHeight : state.top;
  }
  for (const [key, top] of savedDrawers) {
    const drawer = activity.querySelector<HTMLDetailsElement>(`.process-output-drawer[data-drawer-key="${key}"]`);
    const body = drawer?.querySelector<HTMLElement>('.process-drawer-body');
    if (body) body.scrollTop = top;
  }
  if (window.scrollY !== savedWindowScroll) {
    window.scrollTo({ top: savedWindowScroll, behavior: 'instant' as ScrollBehavior });
  }
}
function sandboxAbstractionPanel(id: string, records: Event[]): HTMLElement {
  const job = findJob(id);
  const container = el('div', 'sandbox-abstraction');

  const header = el('div', 'sandbox-vm-header');
  const left = el('div', 'sandbox-vm-title-group');
  const titleText = el('span', 'sandbox-vm-title', `📦 ${t('Isolated analysis container', '隔离分析容器')}`);

  const isRunning = job ? job.status === 'running' : false;
  const isCompleted = job ? job.status === 'completed' : false;
  const isFailed = job ? job.status === 'failed' : false;
  const isCancelled = job ? job.status === 'cancelled' : false;

  const vmStatusPill = el('span', `sandbox-vm-status ${job?.status ?? 'standby'}`);
  if (isRunning) {
    const dot = el('span', 'heartbeat-dot pulse');
    vmStatusPill.append(dot, document.createTextNode(` ${t('Active', '活跃')}`));
  } else if (isCompleted) {
    vmStatusPill.textContent = `✓ ${t('Completed', '已完成')}`;
  } else if (isFailed) {
    vmStatusPill.textContent = `✕ ${t('Failed', '失败')}`;
  } else if (isCancelled) {
    vmStatusPill.textContent = `⊘ ${t('Cancelled', '已取消')}`;
  } else {
    vmStatusPill.textContent = `⋯ ${t('Standby', '待命')}`;
  }
  left.append(titleText, vmStatusPill);

  const right = el('div', 'sandbox-vm-meta');
  if (job?.resource_plan) {
    const plan = job.resource_plan;
    right.append(el('span', 'sandbox-res-chip', `${plan.profile.toUpperCase()} · ${plan.cpu_milli}m CPU · ${plan.memory_mb}MB RAM · ${plan.disk_mb}MB Disk`));
  } else {
    right.append(el('span', 'sandbox-res-chip', t('Worker container · limits assigned on claim', '工作器容器 · 领取任务时分配限制')));
  }
  header.append(left, right);
  container.append(header);

  const guardBar = el('div', 'sandbox-guard-bar');
  const guardEvents = records.filter(e => e.agent === 'guard');
  const hasInjection = guardEvents.some(e => e.message.toLowerCase().includes('injection'));
  const hasSanitized = guardEvents.some(e => e.message.toLowerCase().includes('refined') || e.message.toLowerCase().includes('sanitized')) || !!job?.sanitized_description;
  const guardFailed = guardEvents.some(e => e.message.toLowerCase().includes('failed'));

  if (hasInjection) {
    guardBar.classList.add('blocked');
    guardBar.textContent = `🛡️ ${t('Security Guard: Injection detected & blocked', '安全防护：检测到提示词注入并拦截')}`;
  } else if (hasSanitized) {
    guardBar.classList.add('sanitized');
    guardBar.textContent = `🛡️ ${t('Security Guard: Incident prompt sanitized & passed to isolated job', '安全防护：提示词已净化并放行到隔离任务')}`;
  } else if (guardFailed) {
    guardBar.classList.add('warning');
    guardBar.textContent = `🛡️ ${t('Security Guard: Pre-check warning', '安全防护：预检警报')}`;
  } else if (job && !['draft', 'queued'].includes(job.status)) {
    guardBar.classList.add('passed');
    guardBar.textContent = `🛡️ ${t('Security Guard: Pre-check passed · prompt & files clean', '安全防护：预检已通过 · 提示词与附件无风险')}`;
  } else {
    guardBar.classList.add('standby');
    guardBar.textContent = `🛡️ ${t('Security Guard: Standby', '安全防护：待命')}`;
  }
  container.append(guardBar);

  const processGrid = el('div', 'sandbox-process-grid');
  const { parent, children } = observedAgents(job, records);
  const roleNames: Record<string, string> = { log_investigator: 'Log Investigator', telemetry_investigator: 'Telemetry Investigator', evidence_reviewer: 'Evidence Reviewer' };
  const agentDefs: Array<{ id: string; name: string; role: string; icon: string; child?: SubagentSummary }> = [
    ...(parent ? [{ id: 'codex', name: 'Orchestrator', role: t('Parent Process', '主分析进程'), icon: '⚡' }] : []),
    ...children.map(child => ({ id: child.thread_id,
      name: child.role ? roleNames[child.role] ?? child.role : 'Subagent',
      role: child.thread_id, icon: '⚙️', child })),
  ];
  if (!agentDefs.length) processGrid.append(el('p', 'muted-note', t('No agent has started yet.', '尚无代理启动。')));

  for (const def of agentDefs) {
    const agentKey = `${id}:${def.id}`;
    const currentEvents = records.filter(e => !isHeartbeat(e) && (def.child
      ? e.subagent?.thread_id === def.id
      : !e.subagent && e.agent === def.id));

    // Retain worker outputs across sliding-window roll-offs (hard limit of 50 to protect memory, while always ensuring at least the last 5 outputs are shown)
    const prevRetained = retainedAgentEvents.get(agentKey) ?? [];
    const eventMap = new Map<number, Event>();
    for (const ev of prevRetained) eventMap.set(ev.seq, ev);
    for (const ev of currentEvents) eventMap.set(ev.seq, ev);
    const allAgentEvents = [...eventMap.values()].sort((a, b) => a.seq - b.seq);
    const agentEvents = allAgentEvents.slice(-50);
    retainedAgentEvents.set(agentKey, agentEvents);

    const card = el('div', `sandbox-process-card ${def.child ? 'subagent' : def.id}`);

    const cardHeader = el('div', 'process-card-header');
    const headerLeft = el('div', 'process-title-group');
    headerLeft.append(el('span', 'process-icon', def.icon), el('span', 'process-name', def.name), el('span', 'process-role', def.role));

    let agentStatus = t('No recorded activity', '无活动记录');
    let agentStatusCls = 'idle';
    if (def.child) {
      const badge = subagentBadge(def.child, !!job && terminal(job));
      agentStatus = t(badge.en, badge.zh);
      agentStatusCls = badge.cls;
    } else if (def.id === 'codex') {
      agentStatus = isRunning ? t('Active', '执行中') : isCompleted ? t('Completed', '已完成') : isFailed || isCancelled ? t('Stopped', '已终止') : t('Standby', '待命');
      agentStatusCls = isRunning ? 'active' : isCompleted ? 'completed' : isFailed || isCancelled ? 'stopped' : 'standby';
    } else if (agentEvents.length) {
      agentStatus = t('Activity recorded · state unknown', '有活动记录 · 状态未知');
    }

    const cardBadge = el('span', `process-badge ${agentStatusCls}`, agentStatus);
    cardHeader.append(headerLeft, cardBadge);
    card.append(cardHeader);

    const latestAction = el('div', 'process-latest-action');
    if (agentEvents.length > 0) {
      latestAction.textContent = agentEvents[agentEvents.length - 1].message;
    } else if (def.child) {
      latestAction.textContent = t('Latest lifecycle state retained in job history.', '任务历史已保留最新生命周期状态。');
    } else if (isRunning) {
      latestAction.textContent = def.id === 'codex' ? t('Orchestrating tasks…', '正在调度任务…') : t('No child activity recorded for this role.', '此角色暂无子进程活动记录。');
    } else {
      latestAction.textContent = t('No activities recorded.', '暂无活动记录。');
    }
    card.append(latestAction);

    const drawer = el('details', 'process-output-drawer');
    const drawerKey = `${id}:${def.id}`;
    drawer.dataset.drawerKey = drawerKey;
    drawer.open = processDrawers.get(drawerKey) ?? false;
    drawer.addEventListener('toggle', () => {
      processDrawers.set(drawerKey, drawer.open);
      const hint = drawer.querySelector('.drawer-toggle-hint');
      if (hint) hint.textContent = drawer.open ? t('Hide', '收起') : t('View', '展开查看');
    });

    const summary = el('summary', 'process-drawer-summary');
    summary.append(
      el('span', 'drawer-summary-label', `${t('Intermediate outputs', '中间输出与记录')} (${agentEvents.length})`),
      el('span', 'drawer-toggle-hint', drawer.open ? t('Hide', '收起') : t('View', '展开查看'))
    );

    const drawerBody = el('div', 'process-drawer-body');
    if (!agentEvents.length) {
      drawerBody.append(el('p', 'muted-note', t('No intermediate outputs from this process yet.', '此进程暂未产生中间输出。')));
    } else {
      // Newest outputs show on top
      for (const ev of [...agentEvents].reverse()) {
        const item = el('div', 'output-item');
        const itemMeta = el('div', 'output-meta');
        itemMeta.append(
          el('span', `output-kind ${ev.kind}`, ev.kind),
          el('span', 'output-time', ev.created_at ? date(ev.created_at) : `#${ev.seq}`)
        );
        const itemMsg = el('pre', 'output-text', ev.message);
        item.append(itemMeta, itemMsg);
        drawerBody.append(item);
      }
    }
    drawer.append(summary, drawerBody);
    card.append(drawer);

    processGrid.append(card);
  }
  if (retainedAgentEvents.size > 100) {
    const activePrefixes = new Set([...active.map(j => `${j.id}:`), ...(selected ? [`${selected}:`] : [])]);
    for (const key of retainedAgentEvents.keys()) {
      if (![...activePrefixes].some(prefix => key.startsWith(prefix))) {
        retainedAgentEvents.delete(key);
      }
    }
  }
  container.append(processGrid);

  const heartbeats = records.filter(isHeartbeat);
  const heartbeatStrip = el('div', 'sandbox-heartbeat-strip');
  const hbDot = el('span', `heartbeat-dot ${isRunning ? 'pulse' : 'done'}`);
  const hbText = el('span', 'heartbeat-text');
  if (isRunning) {
    const lastHb = heartbeats.length ? heartbeats[heartbeats.length - 1] : undefined;
    const timeStr = lastHb?.created_at ? date(lastHb.created_at) : t('Active', '活跃');
    hbText.textContent = `${t('Analysis container healthy', '分析容器运行正常')} · ${t('Recorded', '心跳次数')}: ${heartbeats.length} · ${t('Latest heartbeat', '最新心跳')}: ${timeStr}`;
  } else if (isCompleted) {
    hbText.textContent = t('✓ Analysis container finished; workspace cleanup requested.', '✓ 分析容器已完成；已请求清理工作区。');
  } else if (isFailed) {
    hbText.textContent = t('✕ Analysis container terminated with errors.', '✕ 分析容器已终止（异常退出）。');
  } else if (isCancelled) {
    hbText.textContent = t('⊘ Analysis container cancelled and resources released.', '⊘ 分析容器已取消，资源已释放。');
  } else {
    hbText.textContent = t('⋯ Waiting for a worker to claim this analysis.', '⋯ 等待工作器领取此分析任务。');
  }
  heartbeatStrip.append(hbDot, hbText);
  container.append(heartbeatStrip);

  return container;
}

function sessionPanel(id: string, records: Event[]): HTMLDetailsElement {
  const panel = el('details', 'session-panel'); panel.dataset.job = id; panel.open = sessionViews.get(id)?.open ?? true;
  panel.append(el('summary', '', t('Session activity & output', '会话活动与输出')), el('p', 'session-hint', t('Orchestrator debug output, tool calls, results and child activity. Credentials are redacted. Large outputs continue across entries.', '显示调度器调试输出、工具调用、结果与活动；凭据已脱敏。较长输出会分成连续记录。')));

  // Keep observed lifecycle history visible after the container is removed.
  panel.append(sandboxAbstractionPanel(id, records));

  const transcriptDetails = el('details', 'transcript-collapsible');
  transcriptDetails.open = true;
  transcriptDetails.append(el('summary', 'transcript-summary', t('Detailed Activity Log', '详细活动日志')));
  const download = el('a', 'button text-button', t('Download debug JSONL', '下载调试 JSONL'));
  download.href = `/api/jobs/${encodeURIComponent(id)}/events.jsonl`; download.download = `${id}-events.jsonl`;
  transcriptDetails.append(download);

  const transcript = el('div', 'session-transcript'); transcript.tabIndex = 0; transcript.setAttribute('aria-label', t('Scrollable session activity', '可滚动会话活动'));
  if (!records.length) transcript.append(el('p', 'muted', t('Waiting for worker activity.', '等待工作器活动。')));

  type Grouped = { type: 'event'; event: Event } | { type: 'heartbeat'; count: number; first: Event; last: Event };
  const grouped: Grouped[] = [];
  for (const event of records) {
    if (isHeartbeat(event)) {
      const prev = grouped[grouped.length - 1];
      if (prev && prev.type === 'heartbeat') {
        prev.count++;
        prev.last = event;
      } else {
        grouped.push({ type: 'heartbeat', count: 1, first: event, last: event });
      }
    } else {
      grouped.push({ type: 'event', event });
    }
  }

  for (const item of grouped) {
    if (item.type === 'event') {
      const line = el('div', 'session-entry');
      line.append(el('span', 'session-agent', `${item.event.agent} · ${item.event.kind}${item.event.created_at ? ` · ${date(item.event.created_at)}` : ''}`), el('p', 'session-message', item.event.message));
      transcript.append(line);
    } else {
      const line = el('div', 'session-entry heartbeat-entry');
      if (item.count === 1) {
        line.append(el('span', 'session-agent', `${item.first.agent} · heartbeat${item.first.created_at ? ` · ${date(item.first.created_at)}` : ''}`), el('p', 'session-message heartbeat-message', item.first.message));
      } else {
        line.append(el('span', 'session-agent', `${item.first.agent} · heartbeat · ${item.count} ticks`), el('p', 'session-message heartbeat-message', `● ${t(`Analysis container heartbeat active (${item.count} ticks collapsed)`, `分析容器持续心跳中（已合并 ${item.count} 条心跳）`)}${item.last.created_at ? ` · ${date(item.last.created_at)}` : ''}`));
      }
      transcript.append(line);
    }
  }

  transcriptDetails.append(transcript, el('p', 'session-hint', pausedSessions.has(id) ? t('Reading earlier activity · live updates paused for this view', '正在查看更早活动 · 此视图已暂停实时更新') : t('Up to 500 recent events · updates every 3 seconds while running', '最多显示 500 条最近活动 · 运行时每 3 秒更新')));
  const controls = el('div', 'session-controls');
  if (records.length === 500) controls.append(button(t('Earlier activity', '更早活动'), 'button text-button compact', () => { void sessionPage(id, records[0].seq); }));
  if (pausedSessions.has(id)) controls.append(button(t('Back to latest', '返回最新'), 'button text-button compact', () => { void sessionPage(id); }));
  transcriptDetails.append(controls);

  panel.append(transcriptDetails);
  return panel;
}
async function sessionPage(id: string, before?: number): Promise<void> {
  try {
    const reply = await api<{ events: Event[] }>(`/jobs/${id}/events?${before ? `before=${before}` : 'latest=true'}`);
    if (before) pausedSessions.add(id); else pausedSessions.delete(id);
    events.set(id, reply.events); sessionViews.set(id, { open: true, top: 0, following: !before });
    const old = document.querySelector<HTMLDetailsElement>(`.session-panel[data-job="${id}"]`);
    if (old) { const replacement = sessionPanel(id, reply.events); old.replaceWith(replacement); const transcript = replacement.querySelector<HTMLElement>('.session-transcript')!; if (!before) transcript.scrollTop = transcript.scrollHeight; }
  } catch (error) { notice(`${t('Could not load session activity', '无法加载会话活动')}：${error}`, true); }
}
async function stopJob(job: Job): Promise<void> {
  if (!window.confirm(t(`Stop “${title(job)}”? This cancels the entire analysis.`, `停止“${title(job)}”？此操作将取消整个分析。`))) return;
  try { await api(`/jobs/${job.id}/cancel`, { method: 'POST' }); notice(t('Stop requested. Running work stops when the worker confirms termination.', '已请求停止。工作器确认终止后，运行任务才会结束。')); await refresh(); }
  catch (error) { notice(`${t('Could not stop analysis', '无法停止分析')}：${error}`, true); }
}
function showNew(): void { selected = null; selectedJob = undefined; detailSequence++; detailSignature = ''; notice(''); renderHistory(); renderNew(); window.scrollTo({ top: 0 }); const heading = content.querySelector('h1'); if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); } }
function addFiles(incoming: FileList | File[]): void {
  if (busy) return;
  const all = new Map(files.map(file => [`${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`, file]));
  for (const file of Array.from(incoming)) all.set(`${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`, file);
  files = [...all.values()]; renderNew();
}
function renderNew(): void {
  content.replaceChildren(); const page = el('section', 'new-page');
  page.append(el('p', 'eyebrow', t('FROM INCIDENT TO NEXT STEPS', '从现场问题到解决步骤')), el('h1', '', t('What happened to your robot?', '机器人遇到了什么问题？')), el('p', 'intro', t('Share the files and describe the scene. We’ll connect the evidence and suggest what to do next.', '上传文件，描述现场。我们将整理证据，并建议下一步如何解决。')));
  const form = el('form', 'upload-form'), drop = el('div', 'drop-zone');
  drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('dragging'); }); drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
  drop.addEventListener('drop', event => { event.preventDefault(); drop.classList.remove('dragging'); if (event.dataTransfer) addFiles(event.dataTransfer.files); });
  const fileInput = el('input'); fileInput.type = 'file'; fileInput.multiple = true; fileInput.hidden = true; fileInput.id = 'upload-files'; fileInput.disabled = busy;
  fileInput.addEventListener('change', () => { if (fileInput.files) addFiles(fileInput.files); });
  const folderInput = el('input'); folderInput.type = 'file'; folderInput.multiple = true; folderInput.setAttribute('webkitdirectory', ''); folderInput.hidden = true; folderInput.id = 'upload-folder'; folderInput.disabled = busy;
  folderInput.addEventListener('change', () => { if (folderInput.files) addFiles(folderInput.files); });
  drop.append(el('span', 'upload-symbol', '↥'), el('h2', '', t('Drop your files here', '将文件拖到这里')), el('p', 'muted', t('Compressed logs, images, PDFs, or a folder', '压缩日志、图片、PDF，或文件夹')));
  const pickers = el('div', 'picker-actions');
  for (const [label, target] of [[t('Choose files', '选择文件'), fileInput], [t('Choose folder', '选择文件夹'), folderInput]] as const) { const pick = button(label, 'button secondary', () => target.click()); pick.disabled = busy; pickers.append(pick); }
  drop.append(pickers, fileInput, folderInput); form.append(drop);
  if (files.length) {
    const selection = el('div', 'file-selection'); selection.append(el('strong', '', t(`${files.length} files · ${size(files.reduce((n, f) => n + f.size, 0))}`, `${files.length} 个文件 · ${size(files.reduce((n, f) => n + f.size, 0))}`)));
    const clear = button(t('Clear', '清除'), 'button text-button compact', () => { files = []; renderNew(); }); clear.disabled = busy; selection.append(clear); form.append(selection);
    const names = el('div', 'file-chips'); for (const file of files.slice(0, 5)) names.append(el('span', 'file-chip', file.webkitRelativePath || file.name)); if (files.length > 5) names.append(el('span', 'file-chip', `+${files.length - 5}`)); form.append(names);
  }
  const descriptionInput = el('textarea'); descriptionInput.id = 'incident-description'; descriptionInput.rows = 4; descriptionInput.maxLength = 8000; descriptionInput.value = description; descriptionInput.disabled = busy;
  descriptionInput.placeholder = t('For example: the robot stopped near the doorway around 10:30. It recovered after a restart, but the issue happened twice.', '例如：机器人在 10:30 左右停在门口。重启后恢复，但同样的问题发生了两次。'); descriptionInput.addEventListener('input', () => { description = descriptionInput.value; });
  form.append(field(t('Describe what happened', '描述发生了什么'), descriptionInput));
  const bottom = el('div', 'form-bottom'), language = el('select'); language.id = 'output-language';
  for (const [value, label] of [['en', 'English'], ['zh', '中文']]) { const option = el('option', '', label); option.value = value; language.append(option); }
  language.value = outputLanguage; language.disabled = busy; language.addEventListener('change', () => { outputLanguage = language.value as 'en' | 'zh'; outputChosen = true; });
  const start = el('button', 'button primary start-analysis', submissionLabel()); start.type = 'submit'; start.disabled = busy || !availability(budget).canSubmit;
  bottom.append(field(t('Report language', '报告语言'), language), start); form.append(bottom);
  form.append(el('p', 'privacy-note', t('Submitting uploads the selected files to this shared workspace. Everyone can see the analysis and review it.', '提交后，所选文件将上传到共享工作台。所有人都可以查看分析并进行审核。')));
  form.addEventListener('submit', event => { event.preventDefault(); void submit(); }); page.append(form);
  const demoJobs = jobs.filter(job => job.id.startsWith('demo-'));
  if (demoJobs.length) { const demos = el('div', 'demo-examples'); demos.append(el('span', 'muted', t('Want to explore first? Synthetic examples:', '先看看效果？以下为合成示例：'))); for (const job of demoJobs) demos.append(button(title(job), 'example-link', () => { void selectJob(job.id); })); page.append(demos); }
  const sample = el('a', 'example-link', t('Download a synthetic log to try uploading', '下载合成日志，体验上传')); sample.href = '/examples/doorway-stop.txt'; sample.download = 'doorway-stop.txt'; page.append(sample);
  content.append(page);
}
function renderUpload(): void {
  if (!uploadPanel) return;
  uploadPanel.hidden = !busy && !submitMessage;
  // Keep the cancel button focused and the progress element animated across ticks.
  if (!uploadPanel.firstChild) {
    const text = el('p', 'submit-progress'); text.id = 'submit-progress'; text.setAttribute('role', 'status');
    const bar = el('progress', 'upload-byte-progress'); bar.max = 1; bar.setAttribute('aria-label', t('Current file upload', '当前文件上传'));
    const warning = el('p', 'upload-stall'); warning.setAttribute('role', 'status');
    const wake = el('p', 'upload-wake');
    uploadPanel.append(text, bar, warning, wake, button(t('Cancel upload', '取消上传'), 'button text-button', () => controller?.abort()));
  }
  const text = uploadPanel.querySelector<HTMLElement>('.submit-progress')!;
  const bar = uploadPanel.querySelector<HTMLProgressElement>('progress')!;
  bar.hidden = !transfer;
  if (transfer) {
    const p = transfer, percent = p.total ? Math.floor(p.loaded / p.total * 100) : p.awaitingResponse ? 100 : 0;
    const bytes = `${(p.loaded / 1e6).toFixed(1)} MB / ${(p.total / 1e6).toFixed(1)} MB · ${percent}%`;
    const speed = `${(p.bytesPerSecond / 1e6).toFixed(1)} MB/s`;
    const eta = p.remainingSeconds === null ? t('Estimating time…', '正在估算剩余时间…') : t(`About ${p.remainingSeconds}s remaining`, `预计剩余 ${p.remainingSeconds} 秒`);
    text.textContent = t(`Uploading ${p.index}/${p.count}: ${p.name}`, `正在上传 ${p.index}/${p.count}：${p.name}`) + ` (${bytes}) · ` +
      (p.awaitingResponse ? t('Bytes sent; waiting for server confirmation…', '文件已发送，正在等待服务器确认…') : `${speed} · ${eta}`);
    bar.max = Math.max(1, p.total); bar.value = p.total ? p.loaded : p.awaitingResponse ? 1 : 0;
    bar.setAttribute('aria-valuetext', `${p.loaded.toLocaleString()} / ${p.total.toLocaleString()} bytes`);
  } else text.textContent = submitMessage;
  uploadPanel.classList.toggle('stalled', Boolean(transfer?.stalled));
  const warning = uploadPanel.querySelector<HTMLElement>('.upload-stall')!;
  warning.hidden = !transfer?.stalled;
  warning.textContent = t('⚠️ No upload progress for 10 seconds. Waiting for the connection to recover…', '⚠️ 网络传输停滞，正在等待连接恢复...');
  const wake = uploadPanel.querySelector<HTMLElement>('.upload-wake')!;
  wake.hidden = !busy;
  wake.textContent = wakeState === 'active' ? t('Keeping the screen awake. Keep this tab visible while uploading.', '已保持屏幕常亮。上传期间请保持此标签页可见。') :
    wakeState === 'requesting' ? t('Requesting screen wake lock…', '正在请求屏幕常亮…') :
    t('Screen wake lock is unavailable. Keep this tab visible and prevent the device from sleeping.', '屏幕常亮不可用。请保持此标签页可见，并避免设备休眠。');
  uploadPanel.querySelector<HTMLButtonElement>('button')!.hidden = !busy;
}
function progress(message: string): void { submitMessage = message; renderUpload(); }
function prepare(logs: File[], signal: AbortSignal): Promise<LogPackage> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./preprocess.worker.ts', import.meta.url), { type: 'module' });
    const abort = () => { worker.terminate(); reject(new DOMException('Cancelled', 'AbortError')); };
    signal.addEventListener('abort', abort, { once: true });
    const finish = () => { worker.terminate(); signal.removeEventListener('abort', abort); };
    worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
      if (event.data.type === 'complete') { finish(); resolve(event.data.result); }
      if (event.data.type === 'error') { finish(); reject(new Error(event.data.message)); }
    };
    worker.onerror = () => { finish(); reject(new Error(t('The files could not be prepared.', '无法准备这些文件。'))); };
    worker.postMessage({ type: 'start', files: logs, limits: DEFAULT_LIMITS });
  });
}
async function submit(): Promise<void> {
  if (busy) return;
  if (!files.length || !description.trim()) { notice(t('Choose files and describe the incident first.', '请先选择文件并描述问题。'), true); return; }
  if (files.some(file => file.size > 2 * 1024 ** 3) || files.reduce((n, file) => n + file.size, 0) > 4 * 1024 ** 3) { notice(t('Upload limit: 2 GiB per file and 4 GiB per analysis.', '上传限制：每个文件 2 GiB，每次分析共 4 GiB。'), true); return; }
  busy = true; transfer = undefined; wakeState = 'requesting'; controller = new AbortController(); const signal = controller.signal; let createdId: string | undefined;
  notice(''); progress(t('Preparing your files…', '正在准备文件…')); renderNew();
  const stopProtection = protectUpload(state => { wakeState = state; renderUpload(); });
  try {
    budget = await api<Budget>('/budget', { signal }); renderBudget();
    if (!availability(budget).canSubmit) throw new Error(t('No pending slot is available. Your upload draft is preserved.', '暂无待处理空位，上传草稿已保留。'));
    const media = (file: File) => /\.(pdf|png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name) || /^(image\/|application\/pdf)/.test(file.type);
    const logs = files.filter(file => !media(file)), attachments = files.filter(media);
    const evidence = logs.length ? await prepare(logs, signal) : undefined;
    signal.throwIfAborted(); progress(t('Uploading to the workspace…', '正在上传到工作台…'));
    // Once creation starts, retain its response even if the user cancels, so
    // the newly allocated draft can be cancelled using its server ID.
    const created = await api<{ job: Job; upload_token: string }>('/jobs', { ...json({ description: description.trim(), language: outputLanguage, evidence }), signal: AbortSignal.timeout(30_000) }); createdId = created.job.id;
    signal.throwIfAborted();
    const ordered = [...logs, ...attachments];
    for (let index = 0; index < ordered.length; index++) {
      const file = ordered[index], name = file.webkitRelativePath || file.name;
      await uploadFile(`/api/jobs/${createdId}/files?name=${encodeURIComponent(name)}`, file, created.upload_token, signal,
        state => { transfer = { ...state, index: index + 1, count: ordered.length, name }; renderUpload(); });
    }
    signal.throwIfAborted(); transfer = undefined; progress(t('Confirming submission…', '正在确认提交…'));
    await api(`/jobs/${createdId}/submit`, { ...json({}, created.upload_token), signal });
    files = []; description = ''; progress(''); await selectJob(createdId); await refresh();
  } catch (error) {
    transfer = undefined;
    if (createdId) { try { await api(`/jobs/${createdId}/cancel`, { method: 'POST', signal: AbortSignal.timeout(10_000) }); } catch { /* The shared history retains failed upload state for inspection. */ } }
    const message = signal.aborted ? t('Upload cancelled. Your originals are unchanged.', '上传已取消，原始文件未改变。') : `${t('Could not submit analysis', '无法提交分析')}：${error}`;
    progress(message); notice(message, !signal.aborted);
  } finally { stopProtection(); busy = false; transfer = undefined; controller = undefined; renderUpload(); if (!selected) renderNew(); }
}
async function selectJob(id: string): Promise<void> { selected = id; selectedJob = undefined; detailSignature = ''; detailNeedsRender = true; notice(''); renderHistory(); content.replaceChildren(el('p', 'loading', t('Opening analysis…', '正在打开分析…'))); await loadDetail(true); }
async function loadDetail(force = false): Promise<void> {
  const id = selected; if (!id) return; const sequence = ++detailSequence;
  try {
    const [job, versions, activityPage] = await Promise.all([api<Job>(`/jobs/${id}`), api<Version[]>(`/jobs/${id}/versions`), api<{ events: Event[] }>(`/jobs/${id}/events?latest=true`)]);
    if (selected !== id || sequence !== detailSequence) return;
    selectedJob = job; selectedVersions = versions; selectedEvents = activityPage.events; mergeJobs([job]); renderHistory();
    const draft = drafts.get(id);
    if (draft?.token && (draft.expires_at !== job.review_claim?.expires_at || draft.name !== job.review_claim?.name || Date.parse(draft.expires_at ?? '') <= Date.now())) {
      draft.token = undefined; draft.expires_at = undefined; localStorage.removeItem(claimKey(id));
      notice(t('Your editing reservation expired. Your draft is preserved; reserve editing again to save.', '编辑锁定已过期。草稿已保留，请重新申请编辑后保存。')); force = true;
    }
    if (!pausedSessions.has(id)) events.set(id, selectedEvents);
    const signature = JSON.stringify([job.status, job.cancel_requested, job.report, job.review_claim, job.codex_started, job.subagents, selectedEvents.at(-1)?.seq, versions]);
    if (force || detailNeedsRender || signature !== detailSignature) { detailSignature = signature; if (force || detailNeedsRender || !drafts.has(id)) { renderResult(); detailNeedsRender = false; } }
  } catch (error) { if (selected === id) notice(`${t('Could not open analysis', '无法打开分析')}：${error}`, true); }
}
function reportSection(number: string, name: string): HTMLElement { const section = el('section', 'report-section'); const heading = el('div', 'report-section-heading'); heading.append(el('span', 'section-number', number), el('h2', '', name)); section.append(heading); return section; }
function renderResult(): void {
  const job = selectedJob; if (!job || selected !== job.id) return;
  // Preserve scroll positions before blowing away the DOM.
  const savedWindowScroll = window.scrollY;
  const savedContentScroll = content.scrollTop;
  const existingTranscript = content.querySelector<HTMLElement>('.session-transcript');
  if (existingTranscript) {
    sessionViews.set(job.id, {
      open: content.querySelector<HTMLDetailsElement>('.session-panel')?.open ?? true,
      top: existingTranscript.scrollTop,
      following: existingTranscript.scrollHeight - existingTranscript.scrollTop - existingTranscript.clientHeight < 30,
    });
  }
  const savedDrawers = new Map<string, number>();
  for (const drawer of content.querySelectorAll<HTMLDetailsElement>('.process-output-drawer')) {
    const body = drawer.querySelector<HTMLElement>('.process-drawer-body');
    if (body && drawer.dataset.drawerKey) savedDrawers.set(drawer.dataset.drawerKey, body.scrollTop);
  }
  content.replaceChildren(); const page = el('article', 'result-page');
  const navigation = el('div', 'report-navigation'); navigation.append(button(t('← Back to upload', '← 返回上传'), 'button secondary back-to-upload', showNew)); page.append(navigation);
  const meta = el('div', 'report-meta'); meta.append(statusBadge(job), el('span', 'muted', date(job.created_at))); page.append(meta, el('h1', '', title(job)), el('p', 'incident-description', job.description.replace(/^\[DEMO\]\s*/, '')));
  if (job.resource_plan) {
    const plan = job.resource_plan;
    page.append(el('p', 'sandbox-profile', t(`Auto-sized container plan (${plan.profile}): ${plan.cpu_milli / 1000} CPU · ${plan.memory_mb / 1024} GiB RAM · ${plan.disk_mb / 1024} GiB monitored disk. Estimated from uploads, not guaranteed workload usage.`, `自动选择容器计划（${plan.profile}）：CPU ${plan.cpu_milli / 1000} 核 · 内存 ${plan.memory_mb / 1024} GiB · 监控磁盘 ${plan.disk_mb / 1024} GiB。依据上传内容估算，并非实际用量保证。`)));
  }
  if (!terminal(job)) { page.append(el('div', 'waiting-panel', job.status === 'queued' ? t('Your analysis is queued. It starts when a worker and budget are available. You can leave this page and return from history.', '分析已排队。有可用工作器和预算时会开始。你可以离开本页，稍后从历史记录返回。') : t('Analysis is in progress. Everyone can follow the activity above.', '分析正在进行，所有人都可以在上方查看活动。'))); content.append(page); return; }
  const report = parseReport(job.report);
  if (report.demo) page.append(el('div', 'demo-notice', t('Synthetic demo · no AI agent ran. Use this example to test the evidence and editing experience.', '合成示例 · 没有运行 AI 代理。此示例用于测试证据展示和编辑体验。')));
  const summary = reportSection('01', t('What we found', '观察到的情况')); summary.append(el('p', 'summary-text', report.summary || t('This analysis ended without a report.', '此次分析结束时未生成报告。'))); page.append(summary);
  const chain = reportSection('02', t('Evidence chain', '证据链'));
  if (!report.evidenceChain.length) chain.append(el('p', 'muted', t('No structured evidence chain was provided. No supporting evidence has been inferred.', '尚未提供结构化证据链。这里不会推断或补造支持证据。')));
  const chainList = el('ol', 'evidence-chain');
  for (const evidence of report.evidenceChain) {
    const item = el('li', 'evidence-item'), heading = el('div', 'evidence-heading'); heading.append(el('span', 'evidence-id', evidence.id), el('h3', '', evidence.observation)); item.append(heading);
    item.append(el('p', 'source-ref', `${evidence.source}${evidence.lines ? ` · ${t('lines', '行')} ${evidence.lines}` : ''}`));
    if (evidence.excerpt) item.append(el('pre', 'evidence-excerpt', evidence.excerpt)); if (evidence.reasoning) item.append(el('p', 'evidence-reasoning', evidence.reasoning)); chainList.append(item);
  }
  chain.append(chainList); page.append(chain);
  if (report.uncertainties.length) { const caveats = el('aside', 'uncertainties'); caveats.append(el('h3', '', t('What still needs confirming', '仍需确认的事项'))); const list = el('ul'); for (const value of report.uncertainties) list.append(el('li', '', value)); caveats.append(list); page.append(caveats); }
  const workflow = reportSection('03', t('Suggested resolution workflow', '建议解决流程')); const latest = selectedVersions.at(-1);
  if (latest) { const review = el('p', 'review-verdict', `${latest.success ? t('Marked successful', '标记为成功') : t('Marked unsuccessful', '标记为未成功')} · ${latest.reviewer_name} · v${latest.id}`); workflow.append(review, el('p', 'muted', latest.note)); }
  const currentWorkflow = latest?.procedure ?? report.workflow.map((step, index) => `${index + 1}. ${step.replace(/^\d+[.)]\s+/, '')}`).join('\n\n');
  let draft = drafts.get(job.id);
  const claim = savedClaim(job);
  if (!draft && claim) { draft = { ...claim, procedure: currentWorkflow, note: '', verdict: '' }; drafts.set(job.id, draft); }
  if (draft) renderEditor(workflow, job, draft);
  else {
    if (latest) workflow.append(el('p', 'workflow-text', latest.procedure));
    else { const steps = el('ol', 'workflow-steps'); for (const step of report.workflow) steps.append(el('li', '', step.replace(/^\d+[.)]\s+/, ''))); workflow.append(steps); }
    if (!currentWorkflow) workflow.append(el('p', 'muted', t('No workflow was provided. A reviewer can add one.', '尚未提供流程，审核人可以补充。')));
    if (job.review_claim) workflow.append(el('p', 'lock-notice', t(`Being edited by ${job.review_claim.name}. Other visitors can read but cannot edit.`, `${job.review_claim.name} 正在编辑。其他访问者可以查看，但不能修改。`)));
    else if (job.status === 'completed') workflow.append(button(t('Edit workflow & review', '编辑流程并审核'), 'button secondary edit-workflow', () => { drafts.set(job.id, { name: '', procedure: currentWorkflow, note: '', verdict: '' }); renderResult(); }));
  }
  page.append(workflow);
  if (selectedVersions.length) { const revisions = el('details', 'revision-history'); revisions.append(el('summary', '', t(`Review history (${selectedVersions.length} versions)`, `审核历史（${selectedVersions.length} 个版本）`))); for (const version of [...selectedVersions].reverse()) { const item = el('div', 'revision'); item.append(el('strong', '', `v${version.id} · ${version.reviewer_name} · ${version.success ? t('Successful', '成功') : t('Unsuccessful', '未成功')}`), el('p', 'muted', date(version.created_at)), el('p', '', version.note), el('p', 'workflow-text', version.procedure)); revisions.append(item); } page.append(revisions); }
  page.append(sessionPanel(job.id, events.get(job.id) ?? selectedEvents), button(t('← Back to upload', '← 返回上传'), 'button secondary back-to-upload', showNew)); content.append(page);
  // Restore page scroll and inner transcript scroll after DOM rebuild.
  content.scrollTop = savedContentScroll;
  if (window.scrollY !== savedWindowScroll) {
    window.scrollTo({ top: savedWindowScroll, behavior: 'instant' as ScrollBehavior });
  }
  const newTranscript = content.querySelector<HTMLElement>('.session-transcript');
  if (newTranscript) {
    const state = sessionViews.get(job.id);
    newTranscript.scrollTop = !state || state.following ? newTranscript.scrollHeight : state.top;
  }
  for (const [key, top] of savedDrawers) {
    const drawer = content.querySelector<HTMLDetailsElement>(`.process-output-drawer[data-drawer-key="${key}"]`);
    const body = drawer?.querySelector<HTMLElement>('.process-drawer-body');
    if (body) body.scrollTop = top;
  }
}
function renderEditor(section: HTMLElement, job: Job, draft: Draft): void {
  const form = el('form', 'workflow-editor');
  if (!draft.token) {
    const name = el('input'); name.value = draft.name; name.required = true; name.maxLength = 100; name.autocomplete = 'name'; name.addEventListener('input', () => { draft.name = name.value; });
    form.append(el('p', 'muted', t('Enter your name to reserve editing. Saving creates a new version and releases the lock.', '输入姓名以独占编辑。保存时会创建新版本并释放锁定。')), field(t('Your name', '你的姓名'), name));
    const claim = el('button', 'button primary', t('Start editing', '开始编辑')); claim.type = 'submit'; form.append(claim);
    form.addEventListener('submit', event => { event.preventDefault(); claim.disabled = true; void api<{ claim_token: string; name: string; expires_at: string }>(`/jobs/${job.id}/claim`, json({ name: draft.name.trim() })).then(async reply => {
      if (selected !== job.id || drafts.get(job.id) !== draft) { await api(`/jobs/${job.id}/claim`, { method: 'DELETE', headers: { Authorization: `Bearer ${reply.claim_token}` } }); return; }
      draft.token = reply.claim_token; draft.name = reply.name; draft.expires_at = reply.expires_at; job.review_claim = { name: reply.name, expires_at: reply.expires_at };
      localStorage.setItem(claimKey(job.id), JSON.stringify({ token: reply.claim_token, name: reply.name, expires_at: reply.expires_at })); renderResult();
    }).catch(error => { claim.disabled = false; notice(`${t('Editing is unavailable (another reviewer may hold the lock)', '无法编辑（可能已有其他审核人锁定）')}：${error}`, true); }); });
  } else {
    form.append(el('p', 'editor-owner', t(`Editing as ${draft.name} · only you can change this version`, `编辑人：${draft.name} · 仅你可以修改此版本`)));
    const procedure = el('textarea'); procedure.rows = 9; procedure.value = draft.procedure; procedure.required = true; procedure.maxLength = 20000; procedure.addEventListener('input', () => { draft.procedure = procedure.value; });
    const verdict = el('select'); for (const [value, label] of [['', t('Choose an outcome', '选择结果')], ['true', t('Successful', '成功')], ['false', t('Not successful', '未成功')]]) { const option = el('option', '', label); option.value = value; verdict.append(option); } verdict.required = true; verdict.value = draft.verdict; verdict.addEventListener('change', () => { draft.verdict = verdict.value; });
    const note = el('textarea'); note.rows = 3; note.value = draft.note; note.required = true; note.maxLength = 5000; note.addEventListener('input', () => { draft.note = note.value; });
    form.append(field(t('Resolution workflow', '解决流程'), procedure), field(t('Did this analysis help solve the issue?', '本次分析是否帮助解决了问题？'), verdict), field(t('Why? What did you verify?', '为什么？你验证了什么？'), note));
    const save = el('button', 'button primary', t('Save review & new version', '保存审核及新版本')); save.type = 'submit'; form.append(save);
    form.addEventListener('submit', event => { event.preventDefault(); save.disabled = true; void api(`/jobs/${job.id}/versions`, json({ success: draft.verdict === 'true', note: draft.note.trim(), procedure: draft.procedure.trim() }, draft.token)).then(async () => { drafts.delete(job.id); localStorage.removeItem(claimKey(job.id)); detailSignature = ''; await loadDetail(); await refresh(); notice(t('Review saved. Your version is available to everyone.', '审核已保存，所有人都可以查看此版本。')); }).catch(error => { save.disabled = false; notice(`${t('Could not save review', '无法保存审核')}：${error}`, true); }); });
  }
  form.append(button(t('Cancel editing', '取消编辑'), 'button text-button', () => { void (async () => { if (draft.token) { try { await api(`/jobs/${job.id}/claim`, { method: 'DELETE', headers: { Authorization: `Bearer ${draft.token}` } }); } catch { /* Expired tokens can still be discarded locally. */ } } drafts.delete(job.id); localStorage.removeItem(claimKey(job.id)); detailSignature = ''; await loadDetail(); })(); })); section.append(form);
}
async function refresh(): Promise<void> {
  if (refreshing) return; refreshing = true;
  try {
    const [page, activePage, currentBudget] = await Promise.all([api<{ items: Job[]; next_cursor: string | null }>('/jobs?limit=50'), api<{ items: Job[] }>('/jobs/active'), api<Budget>('/budget').catch(() => null)]);
    budget = currentBudget; renderBudget();
    const firstLoad = jobs.length === 0;
    const disappeared = active.filter(job => !activePage.items.some(current => current.id === job.id));
    const finished = await Promise.all(disappeared.map(job => api<Job>(`/jobs/${job.id}`).catch(() => job)));
    mergeJobs([...page.items, ...activePage.items, ...finished]); if (!historyInitialized) { cursor = page.next_cursor; historyInitialized = true; } active = activePage.items;
    if (!online) notice(''); online = true;
    await Promise.all(active.map(async job => { if (pausedSessions.has(job.id)) return; const after = events.get(job.id)?.at(-1)?.seq; try { const update = await api<{ events: Event[] }>(`/jobs/${job.id}/events?${after ? `after=${after}` : 'latest=true'}`); events.set(job.id, [...(after ? events.get(job.id) ?? [] : []), ...update.events].slice(-500)); } catch { /* Keep previous activity on transient failures. */ } }));
    updateConnection(); renderHistory(); renderActivity();
    if (firstLoad && !selected && !busy && !description && !files.length) renderNew();
    if (selected) await loadDetail();
  } catch { online = false; budget = null; renderBudget(); updateConnection(); notice(t('The server is unavailable. Your selected files are still here; please retry when it reconnects.', '服务器暂不可用。所选文件仍保留在此处，请在恢复连接后重试。'), true); }
  finally { refreshing = false; }
}
onUiLanguage(() => { if (!outputChosen) outputLanguage = uiLanguage(); buildShell(); });
buildShell(); void refresh(); window.setInterval(() => { void refresh(); }, 3000);
