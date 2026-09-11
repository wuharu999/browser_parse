import './style.css';
import { DEFAULT_LIMITS, type LogPackage, type WorkerResponse } from './types';
import { parseReport } from './report';
import { availability, type Budget } from './budget';
import { onUiLanguage, setUiLanguage, t, uiLanguage } from './i18n';

type Job = { id: string; description: string; status: string; created_at: string; cancel_requested: boolean; report?: string; resource_plan?: { profile: string; cpu_milli: number; memory_mb: number; disk_mb: number } | null; review_claim?: { name: string; expires_at: string } | null; sanitized_description?: string };
type Version = { id: number; reviewer_name: string; success: boolean; note: string; procedure: string; created_at: string };
type Event = { seq: number; agent: string; kind: string; message: string; created_at?: string };
type Draft = { name: string; procedure: string; note: string; verdict: string; token?: string; expires_at?: string };
const terminal = (job: Job) => ['completed', 'failed', 'cancelled'].includes(job.status);
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = '', text?: string): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag); node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = (text: string, cls: string, action: () => void) => { const node = el('button', cls, text); node.type = 'button'; node.addEventListener('click', action); return node; };
const field = (label: string, input: HTMLElement) => { const node = el('label', 'field'); node.append(el('span', 'field-label', label), input); return node; };
const size = (bytes: number) => bytes < 1048576 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
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
let submitMessage = '', controller: AbortController | undefined, selectedJob: Job | undefined, selectedVersions: Version[] = [];
let selectedEvents: Event[] = [], detailSignature = '', detailSequence = 0, detailNeedsRender = false, refreshing = false;
const drafts = new Map<string, Draft>(), events = new Map<string, Event[]>();
const sessionViews = new Map<string, { open: boolean; top: number; following: boolean }>();
const processDrawers = new Map<string, boolean>();
const pausedSessions = new Set<string>();
const isHeartbeat = (event: Event): boolean => event.kind === 'heartbeat' || event.message.includes('Sandbox execution remains active');
const findJob = (id: string): Job | undefined => (selectedJob?.id === id ? selectedJob : active.find(j => j.id === id) ?? jobs.find(j => j.id === id));
const app = document.querySelector<HTMLDivElement>('#app')!;
let history: HTMLElement, content: HTMLElement, activity: HTMLElement, connection: HTMLElement, banner: HTMLElement, activityCount: HTMLElement, usage: HTMLElement;

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
  content = el('div', 'content'); main.append(topbar, banner, usage, activity, content); shell.append(sidebar, main); app.append(shell);
  renderHistory(); renderActivity(); updateConnection(); renderBudget();
  if (selected && selectedJob) renderResult(); else renderNew();
}
function submissionLabel(): string {
  const state = availability(budget).state;
  return busy ? t('Working…', '处理中…') : state === 'unknown' ? t('Checking availability…', '正在检查额度…') : state === 'queue_full' ? t('Queue full', '队列已满') : state === 'available' ? t('Analyze incident  →', '开始分析  →') : t('Queue analysis  →', '加入分析队列  →');
}
function renderBudget(): void {
  const state = availability(budget), money = (value: number) => `$${value.toFixed(2)}`;
  usage.replaceChildren();
  if (!budget || state.state === 'unknown') {
    usage.append(el('p', 'usage-status', t('Daily allowance unavailable. Checking again; your upload draft is preserved.', '暂时无法读取每日额度，正在重试；上传草稿已保留。')));
  } else {
    const top = el('div', 'usage-heading');
    top.append(el('strong', '', t("Today's analysis allowance", '今日分析额度')), el('span', '', `${money(budget.admission_used_usd)} / ${money(budget.daily_limit_usd)}`));
    const bar = el('div', 'usage-bar'); bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-label', t('Accounted usage and active reservations', '已计入用量和运行预留额度'));
    bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', String(budget.daily_limit_usd || 1)); bar.setAttribute('aria-valuenow', String(Math.min(budget.daily_limit_usd, budget.admission_used_usd)));
    bar.setAttribute('aria-valuetext', `${money(budget.admission_used_usd)} / ${money(budget.daily_limit_usd)}`);
    const settled = el('span', 'usage-settled'), reserved = el('span', 'usage-reserved'); settled.style.width = `${state.settledPercent}%`; reserved.style.width = `${state.reservedPercent}%`; bar.append(settled, reserved);
    const messages: Record<string, string> = {
      available: t('You can submit a new analysis. It starts when a worker is ready.', '可以提交新分析；工作器就绪后开始。'),
      budget_wait: t('You can queue a new analysis, but it must wait for enough allowance to become available.', '可以新建排队任务，但需等待可用额度足够后再开始。'),
      capacity_wait: t('Workers are at capacity. New analyses will queue.', '运行名额已满，新分析将排队。'),
      queue_wait: t('You can submit; queued analyses are ahead of the new task.', '可以提交新分析；已有排队任务将先处理。'),
      queue_full: t('Queue full. Wait for a pending slot before submitting a new analysis.', '队列已满。请等待出现空位后再提交新分析。'),
    };
    usage.append(top, bar, el('p', 'usage-breakdown', t(`Accounted: ${money(budget.spent_usd)} · Running reservations: ${money(budget.active_reservations_usd)} · Remaining: ${money(state.remaining)}`, `已计入：${money(budget.spent_usd)} · 运行预留：${money(budget.active_reservations_usd)} · 剩余：${money(state.remaining)}`)), el('p', 'usage-status', messages[state.state]), el('p', 'usage-note', t(`${state.reservations} job reservations available at ${money(budget.estimated_per_job_usd)} each · ${budget.pending}/${budget.max_pending} pending slots used. Daily reset: ${budget.resets_at.slice(0, 10)} 00:00 (Asia/Shanghai); running reservations carry over. Estimates/reservations, not a verified provider bill.`, `每个任务预留 ${money(budget.estimated_per_job_usd)}，额度可覆盖 ${state.reservations} 个任务 · 待处理名额 ${budget.pending}/${budget.max_pending}。每日重置：${budget.resets_at.slice(0, 10)} 00:00（Asia/Shanghai）；运行中的预留额度跨日保留。这里是估算／预留额度，并非已核实的供应商账单。`)));
    if (budget.resource_pool && budget.resources_used) {
      const pool = budget.resource_pool, used = budget.resources_used;
      usage.append(el('p', 'usage-note', t(`Sandbox capacity reserved: ${used.cpu_milli / 1000}/${pool.cpu_milli / 1000} CPU · ${used.memory_mb / 1024}/${pool.memory_mb / 1024} GiB RAM. Resource reservations, not live utilization.`, `沙箱容量预留：CPU ${used.cpu_milli / 1000}/${pool.cpu_milli / 1000} 核 · 内存 ${used.memory_mb / 1024}/${pool.memory_mb / 1024} GiB。这是容量预留，并非实时利用率。`)));
    }
  }
  const start = content.querySelector<HTMLButtonElement>('.start-analysis');
  if (start) { start.textContent = submissionLabel(); start.disabled = busy || !state.canSubmit; }
}
function updateConnection(): void {
  connection.replaceChildren(el('span', `connection-dot ${online ? 'online' : ''}`), el('span', '', online ? t('Shared with everyone', '所有人共享') : t('Connecting to server…', '正在连接服务器…')));
}
function renderHistory(): void {
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
  for (const panel of activity.querySelectorAll<HTMLDetailsElement>('.session-panel')) {
    const transcript = panel.querySelector<HTMLElement>('.session-transcript')!, state = sessionViews.get(panel.dataset.job!);
    transcript.scrollTop = !state || state.following ? transcript.scrollHeight : state.top;
  }
}
function sandboxAbstractionPanel(id: string, records: Event[]): HTMLElement {
  const job = findJob(id);
  const container = el('div', 'sandbox-abstraction');

  const header = el('div', 'sandbox-vm-header');
  const left = el('div', 'sandbox-vm-title-group');
  const titleText = el('span', 'sandbox-vm-title', `📦 ${t('Cube Sandbox VM', 'Cube 沙箱环境')}`);

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
    right.append(el('span', 'sandbox-res-chip', t('Standard VM · 2000m CPU · 4096MB RAM', '标准沙箱 · 2000m CPU · 4096MB RAM')));
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
    guardBar.textContent = `🛡️ ${t('Security Guard: Incident prompt sanitized & passed to sandbox', '安全防护：提示词已净化并放行入沙箱')}`;
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
  const agentDefs: Array<{ id: string; name: string; role: string; icon: string }> = [
    { id: 'codex', name: 'Codex Orchestrator', role: t('Parent Process', '主分析进程'), icon: '⚡' },
    { id: 'log_investigator', name: 'Log Investigator', role: t('Subagent', '诊断子进程'), icon: '🔍' },
    { id: 'evidence_reviewer', name: 'Evidence Reviewer', role: t('Subagent', '审查子进程'), icon: '📋' },
  ];

  for (const rec of records) {
    if (rec.agent && !['worker', 'guard'].includes(rec.agent) && !agentDefs.some(d => d.id === rec.agent)) {
      agentDefs.push({ id: rec.agent, name: rec.agent, role: t('Agent Process', '分析进程'), icon: '⚙️' });
    }
  }

  for (const def of agentDefs) {
    const agentEvents = records.filter(e => e.agent === def.id && !isHeartbeat(e));
    const card = el('div', `sandbox-process-card ${def.id}`);

    const cardHeader = el('div', 'process-card-header');
    const headerLeft = el('div', 'process-title-group');
    headerLeft.append(el('span', 'process-icon', def.icon), el('span', 'process-name', def.name), el('span', 'process-role', def.role));

    let agentStatus = t('Standby', '待命');
    let agentStatusCls = 'standby';
    if (isRunning) {
      if (def.id === 'codex') {
        agentStatus = t('Active', '执行中');
        agentStatusCls = 'active';
      } else if (agentEvents.length > 0) {
        const last = agentEvents[agentEvents.length - 1];
        if (last.message.includes('completed') || last.kind === 'artifact') {
          agentStatus = t('Completed', '已完成');
          agentStatusCls = 'completed';
        } else {
          agentStatus = t('Active', '执行中');
          agentStatusCls = 'active';
        }
      }
    } else if (isCompleted) {
      if (agentEvents.length > 0 || def.id === 'codex') {
        agentStatus = t('Completed', '已完成');
        agentStatusCls = 'completed';
      } else {
        agentStatus = t('Not invoked', '未调用');
        agentStatusCls = 'idle';
      }
    } else if (isFailed || isCancelled) {
      agentStatus = t('Stopped', '已终止');
      agentStatusCls = 'stopped';
    }

    const cardBadge = el('span', `process-badge ${agentStatusCls}`, agentStatus);
    cardHeader.append(headerLeft, cardBadge);
    card.append(cardHeader);

    const latestAction = el('div', 'process-latest-action');
    if (agentEvents.length > 0) {
      latestAction.textContent = agentEvents[agentEvents.length - 1].message;
    } else if (isRunning) {
      latestAction.textContent = def.id === 'codex' ? t('Orchestrating tasks…', '正在调度任务…') : t('Waiting for subagent task dispatch…', '等待子进程任务分派…');
    } else {
      latestAction.textContent = t('No activities recorded.', '暂无活动记录。');
    }
    card.append(latestAction);

    const drawer = el('details', 'process-output-drawer');
    const drawerKey = `${id}:${def.id}`;
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
      for (const ev of agentEvents) {
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
  container.append(processGrid);

  const heartbeats = records.filter(isHeartbeat);
  const heartbeatStrip = el('div', 'sandbox-heartbeat-strip');
  const hbDot = el('span', `heartbeat-dot ${isRunning ? 'pulse' : 'done'}`);
  const hbText = el('span', 'heartbeat-text');
  if (isRunning) {
    const lastHb = heartbeats.length ? heartbeats[heartbeats.length - 1] : undefined;
    const timeStr = lastHb?.created_at ? date(lastHb.created_at) : t('Active', '活跃');
    hbText.textContent = `${t('Sandbox VM healthy', '沙箱运行正常')} · ${t('Recorded', '心跳次数')}: ${heartbeats.length} · ${t('Latest heartbeat', '最新心跳')}: ${timeStr}`;
  } else if (isCompleted) {
    hbText.textContent = t('✓ Sandbox VM execution finished, environment cleaned up.', '✓ 沙箱虚拟机执行完毕，环境已安全释放。');
  } else if (isFailed) {
    hbText.textContent = t('✕ Sandbox VM execution terminated with errors.', '✕ 沙箱虚拟机已终止（异常退出）。');
  } else if (isCancelled) {
    hbText.textContent = t('⊘ Sandbox VM cancelled and resources freed.', '⊘ 沙箱虚拟机已取消，资源已释放。');
  } else {
    hbText.textContent = t('⋯ Sandbox VM standing by for worker assignment.', '⋯ 沙箱虚拟机待命中，等待分配工作器。');
  }
  heartbeatStrip.append(hbDot, hbText);
  container.append(heartbeatStrip);

  return container;
}

function sessionPanel(id: string, records: Event[]): HTMLDetailsElement {
  const panel = el('details', 'session-panel'); panel.dataset.job = id; panel.open = sessionViews.get(id)?.open ?? true;
  panel.append(el('summary', '', t('Session activity & output', '会话活动与输出')), el('p', 'session-hint', t('Public progress, tool status and output. Private reasoning and raw command output are not shared.', '公开进展、工具状态及输出；不展示私密推理和原始命令输出。')));

  // Only show the Sandbox VM abstraction panel while a job is active.
  // Completed / failed / cancelled history jobs show the transcript directly.
  const job = findJob(id);
  const isActive = !job || job.status === 'running' || job.status === 'queued' || job.status === 'draft';
  if (isActive) panel.append(sandboxAbstractionPanel(id, records));

  const transcriptDetails = el('details', 'transcript-collapsible');
  transcriptDetails.open = true;
  transcriptDetails.append(el('summary', 'transcript-summary', t('Detailed Activity Log', '详细活动日志')));

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
        line.append(el('span', 'session-agent', `${item.first.agent} · heartbeat · ${item.count} ticks`), el('p', 'session-message heartbeat-message', `● ${t(`Sandbox heartbeat active (${item.count} ticks collapsed)`, `沙箱持续心跳中（已合并 ${item.count} 条心跳）`)}${item.last.created_at ? ` · ${date(item.last.created_at)}` : ''}`));
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
  const progress = el('p', 'submit-progress', submitMessage); progress.id = 'submit-progress'; progress.setAttribute('role', 'status'); form.append(progress);
  if (busy) form.append(button(t('Cancel upload', '取消上传'), 'button text-button', () => controller?.abort()));
  form.addEventListener('submit', event => { event.preventDefault(); void submit(); }); page.append(form);
  const demoJobs = jobs.filter(job => job.id.startsWith('demo-'));
  if (demoJobs.length) { const demos = el('div', 'demo-examples'); demos.append(el('span', 'muted', t('Want to explore first? Synthetic examples:', '先看看效果？以下为合成示例：'))); for (const job of demoJobs) demos.append(button(title(job), 'example-link', () => { void selectJob(job.id); })); page.append(demos); }
  const sample = el('a', 'example-link', t('Download a synthetic log to try uploading', '下载合成日志，体验上传')); sample.href = '/examples/doorway-stop.txt'; sample.download = 'doorway-stop.txt'; page.append(sample);
  content.append(page);
}
function progress(message: string): void { submitMessage = message; const target = document.querySelector('#submit-progress'); if (target) target.textContent = message; }
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
  busy = true; controller = new AbortController(); const signal = controller.signal; let createdId: string | undefined;
  notice(''); progress(t('Preparing your files…', '正在准备文件…')); renderNew();
  try {
    budget = await api<Budget>('/budget', { signal }); renderBudget();
    if (!availability(budget).canSubmit) throw new Error(t('No pending slot is available. Your upload draft is preserved.', '暂无待处理空位，上传草稿已保留。'));
    const media = (file: File) => /\.(pdf|png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name) || /^(image\/|application\/pdf)/.test(file.type);
    const logs = files.filter(file => !media(file)), attachments = files.filter(media);
    const evidence = logs.length ? await prepare(logs, signal) : undefined;
    signal.throwIfAborted(); progress(t('Uploading to the workspace…', '正在上传到工作台…'));
    const created = await api<{ job: Job; upload_token: string }>('/jobs', { ...json({ description: description.trim(), language: outputLanguage, evidence }), signal }); createdId = created.job.id;
    const ordered = [...logs, ...attachments];
    for (let index = 0; index < ordered.length; index++) {
      const file = ordered[index]; progress(t(`Uploading ${index + 1} of ${ordered.length}…`, `正在上传 ${index + 1}/${ordered.length}…`));
      await api(`/jobs/${createdId}/files?name=${encodeURIComponent(file.webkitRelativePath || file.name)}`, { method: 'PUT', headers: { Authorization: `Bearer ${created.upload_token}`, 'Content-Type': 'application/octet-stream' }, body: file, signal });
    }
    await api(`/jobs/${createdId}/submit`, { ...json({}, created.upload_token), signal });
    files = []; description = ''; progress(''); await selectJob(createdId); await refresh();
  } catch (error) {
    if (createdId) { try { await api(`/jobs/${createdId}/cancel`, { method: 'POST' }); } catch { /* The shared history retains failed upload state for inspection. */ } }
    const message = signal.aborted ? t('Upload cancelled. Your originals are unchanged.', '上传已取消，原始文件未改变。') : `${t('Could not submit analysis', '无法提交分析')}：${error}`;
    progress(message); notice(message, !signal.aborted);
  } finally { busy = false; controller = undefined; if (!selected) renderNew(); }
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
    const signature = JSON.stringify([job.status, job.cancel_requested, job.report, job.review_claim, versions]);
    if (force || detailNeedsRender || signature !== detailSignature) { detailSignature = signature; if (force || detailNeedsRender || !drafts.has(id)) { renderResult(); detailNeedsRender = false; } }
  } catch (error) { if (selected === id) notice(`${t('Could not open analysis', '无法打开分析')}：${error}`, true); }
}
function reportSection(number: string, name: string): HTMLElement { const section = el('section', 'report-section'); const heading = el('div', 'report-section-heading'); heading.append(el('span', 'section-number', number), el('h2', '', name)); section.append(heading); return section; }
function renderResult(): void {
  const job = selectedJob; if (!job || selected !== job.id) return;
  // Preserve scroll positions before blowing away the DOM.
  const savedContentScroll = content.scrollTop;
  const existingTranscript = content.querySelector<HTMLElement>('.session-transcript');
  if (existingTranscript) {
    sessionViews.set(job.id, {
      open: content.querySelector<HTMLDetailsElement>('.session-panel')?.open ?? true,
      top: existingTranscript.scrollTop,
      following: existingTranscript.scrollHeight - existingTranscript.scrollTop - existingTranscript.clientHeight < 30,
    });
  }
  content.replaceChildren(); const page = el('article', 'result-page');
  const navigation = el('div', 'report-navigation'); navigation.append(button(t('← Back to upload', '← 返回上传'), 'button secondary back-to-upload', showNew)); page.append(navigation);
  const meta = el('div', 'report-meta'); meta.append(statusBadge(job), el('span', 'muted', date(job.created_at))); page.append(meta, el('h1', '', title(job)), el('p', 'incident-description', job.description.replace(/^\[DEMO\]\s*/, '')));
  if (job.resource_plan) {
    const plan = job.resource_plan;
    page.append(el('p', 'sandbox-profile', t(`Auto-sized sandbox (${plan.profile}): ${plan.cpu_milli / 1000} CPU · ${plan.memory_mb / 1024} GiB RAM · ${plan.disk_mb / 1024} GiB disk. Estimated from uploads, not guaranteed workload usage.`, `自动选择沙箱（${plan.profile}）：${plan.cpu_milli / 1000} 核 CPU · ${plan.memory_mb / 1024} GiB 内存 · ${plan.disk_mb / 1024} GiB 磁盘。依据上传内容估算，并非实际用量保证。`)));
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
  const newTranscript = content.querySelector<HTMLElement>('.session-transcript');
  if (newTranscript) {
    const state = sessionViews.get(job.id);
    newTranscript.scrollTop = !state || state.following ? newTranscript.scrollHeight : state.top;
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
