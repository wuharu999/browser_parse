import { t } from './i18n';

export type Question = { id: number | string; request_id?: string; question: string; answer: string; status: 'generating' | 'completed' | 'interrupted'; error?: string | null };
type History = { items: Question[]; next_before?: number | null; active?: Question | null; available: boolean; context_mode?: string };
export type ChatEvent = { type: 'started' | 'answer' | 'done' | 'interrupted'; item: Question };

export async function readAnswer(response: Response, receive: (event: ChatEvent) => void): Promise<void> {
  if (!response.body) throw new Error('Missing response stream');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', ended = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split('\n'); buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line) as ChatEvent;
        if (!event.item || !['started', 'answer', 'done', 'interrupted'].includes(event.type)) throw new Error('Invalid response stream');
        receive(event);
        if (event.type === 'done' || event.type === 'interrupted') ended = true;
      }
      if (done) break;
    }
    if (!ended) throw new Error('Answer stream interrupted');
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

const node = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = '') => {
  const element = document.createElement(tag); element.className = cls; return element;
};

export class QuestionsPanel {
  readonly element = node('section', 'questions-panel');
  readonly input = node('textarea', 'question-input');
  private heading = node('h2');
  private context = node('p', 'muted question-context');
  private transcript = node('div', 'question-transcript');
  private status = node('p', 'question-status');
  private send = node('button', 'button primary send-btn');
  private older = node('button', 'button text-button');
  private items = new Map<number | string, Question>();
  private available = false;
  private contextMode = 'report_only';
  private active: Question | null = null;
  private before: number | null = null;
  private loading = false;
  private streaming = false;
  private loaded = false;
  private error = '';
  private pending: { question: string; request_id: string } | undefined;

  constructor(
    private jobId: string,
    private basePath: string = '/api/jobs',
    private token?: string,
  ) {
    if (this.basePath.includes('grill')) {
      this.element.classList.add('grill-qa-panel');
      this.element.setAttribute('data-session-id', this.jobId);
    }
    const form = node('form', 'question-form');
    this.input.rows = 2; this.input.maxLength = 4000; this.input.required = true;
    this.input.addEventListener('input', () => this.controls());
    this.send.type = 'submit'; this.older.type = 'button';
    this.older.addEventListener('click', () => { void this.poll(true); });
    this.transcript.setAttribute('role', 'log'); this.transcript.setAttribute('aria-live', 'off');
    this.transcript.tabIndex = 0; this.status.setAttribute('role', 'status');
    form.append(this.input, this.send);
    form.addEventListener('submit', event => { event.preventDefault(); void this.submit(this.input.value.trim()); });
    this.element.append(this.heading, this.context, this.older, this.transcript, form, this.status);
    this.labels();
  }

  labels(): void {
    const isGrill = this.basePath.includes('grill');
    this.heading.textContent = isGrill
      ? t('Ask about this scenario report', '询问此场景推演报告')
      : t('Ask about this analysis', '询问此分析');
    this.element.setAttribute('aria-label', this.heading.textContent);
    this.input.setAttribute('aria-label', isGrill
      ? t('Question about this scenario report', '关于此推演报告的问题')
      : t('Question about this analysis', '关于此分析的问题'));
    this.input.placeholder = isGrill
      ? t('Ask about robot capabilities, ROS 2 architecture, or risk mitigations…', '询问机器人能力匹配、ROS 2 架构或风险缓解策略…')
      : t('Ask about a finding or the suggested workflow…', '询问分析发现或建议流程…');
    this.transcript.setAttribute('aria-label', t('Shared questions and answers', '共享问答'));
    this.older.textContent = t('Load earlier questions', '加载更早的问题');
    this.send.textContent = t('Send', '发送');
    this.render();
  }

  async poll(older = false): Promise<void> {
    if (this.loading || this.streaming || !this.element.isConnected) return;
    this.loading = true; this.controls();
    try {
      const url = `${this.basePath}/${this.jobId}/questions${older && this.before ? `?before=${this.before}` : ''}`;
      const headers: Record<string, string> = {};
      if (this.token) {
        headers['Authorization'] = `Bearer ${this.token}`;
      }
      const response = await fetch(url, { headers });
      if (!response.ok) throw new Error('history unavailable');
      const page = await response.json() as History;
      this.available = page.available !== false;
      if (page.context_mode) this.contextMode = page.context_mode;
      this.active = page.active || null;
      if (!this.loaded || older) this.before = page.next_before ?? null;
      for (const item of page.items || []) this.items.set(item.id, item);
      if (page.active) this.items.set(page.active.id, page.active);
      this.loaded = true; this.error = ''; this.render(older);
    } catch {
      this.error = t('Could not load questions. Retrying…', '无法加载问答，正在重试…'); this.controls();
    } finally { this.loading = false; this.controls(); }
  }

  private controls(): void {
    this.input.disabled = this.loaded && !this.available;
    this.send.disabled = !this.loaded || !this.available || this.loading || this.streaming || !!this.active || !this.input.value.trim();
    this.older.hidden = this.before === null;
    const isGrill = this.basePath.includes('grill');
    if (isGrill) {
      this.context.textContent = t(
        'Context: Scenario synthesis, robot selection rationale, constraints matrix, and decision context.',
        '上下文：场景推演合成、机器人选型论证、约束矩阵与决策上下文。'
      );
    } else {
      this.context.textContent = `${this.contextMode === 'report_only' ? t('Report-only context', '仅报告上下文') : t('Report + investigation notes', '报告 + 调查笔记')} · ${t('Shared conversation · saved findings only', '共享对话 · 仅依据已保存的分析')}`;
    }
    const unavailable = isGrill ? t('Q&A is temporarily unavailable. Please try again later.', '问答暂时不可用，请稍后重试。') : t('Q&A is unavailable until the server is configured.', '服务端配置完成后即可使用问答。');
    const ready = isGrill ? t('Ask a follow-up question about your report.', '您可以继续提问，进一步了解评估报告。') : t('No daily question limit. Questions do not use analysis slots.', '提问次数不限，不消耗分析额度。');
    this.status.textContent = this.error || (!this.loaded ? t('Loading questions…', '正在加载问答…') : !this.available ? unavailable : this.streaming || this.active ? t('An answer is being written…', '正在生成回答…') : ready);
    for (const retry of this.transcript.querySelectorAll<HTMLButtonElement>('button')) retry.disabled = !this.available || this.loading || this.streaming || !!this.active;
  }

  private render(older = false): void {
    const top = this.transcript.scrollTop, height = this.transcript.scrollHeight;
    const follow = height - top - this.transcript.clientHeight < 30;
    this.transcript.replaceChildren();
    if (!this.items.size) {
      const empty = node('p', 'muted'); empty.textContent = t('Ask a question to start the shared conversation.', '提出问题，开始共享对话。'); this.transcript.append(empty);
    }
    for (const item of [...this.items.values()].sort((a, b) => String(a.id).localeCompare(String(b.id), undefined, { numeric: true }))) {
      const exchange = node('article', 'question-exchange'); exchange.dataset.questionId = String(item.id);
      const question = node('p', 'question-text'), answer = node('p', 'question-answer');
      question.textContent = item.question;
      answer.textContent = item.answer || (item.status === 'generating' ? t('Writing…', '正在回答…') : t('No answer was received.', '尚未收到回答。'));
      exchange.append(question, answer);
      if (item.status === 'interrupted') {
        const label = node('span', 'question-interrupted'); label.textContent = t('Interrupted · partial answer', '已中断 · 回答不完整');
        const retry = node('button', 'button text-button'); retry.type = 'button'; retry.textContent = t('Retry question', '重新提问');
        retry.addEventListener('click', () => { this.pending = undefined; void this.submit(item.question); });
        exchange.append(label, retry);
      }
      this.transcript.append(exchange);
    }
    this.transcript.scrollTop = older ? top + this.transcript.scrollHeight - height : follow ? this.transcript.scrollHeight : top;
    this.controls();
  }

  private async submit(question: string): Promise<void> {
    if (!question || this.streaming || this.loading || this.active || !this.available) return;
    // Keep this ID on ambiguous network failures; only an explicit new attempt
    // after a known interrupted answer gets a new ID.
    if (this.pending?.question !== question) this.pending = { question, request_id: requestId() };
    const pending = this.pending;
    this.streaming = true; this.error = ''; this.controls();
    let acknowledged = false;
    const receive = (item: Question) => {
      acknowledged = true;
      if (this.input.value.trim() === question) this.input.value = '';
      this.items.set(item.id, item); this.active = item.status === 'generating' ? item : null;
      this.render();
    };
    try {
      const url = `${this.basePath}/${this.jobId}/questions`;
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (this.token) {
        headers['Authorization'] = `Bearer ${this.token}`;
      }
      const response = await fetch(url, { method: 'POST', headers, body: JSON.stringify(pending) });
      if (!response.ok) {
        if (response.status === 409) this.pending = undefined;
        throw new Error('request failed');
      }
      if (response.headers.get('content-type')?.includes('application/x-ndjson')) await readAnswer(response, event => receive(event.item));
      else receive((await response.json() as { item: Question }).item);
      this.pending = undefined;
    } catch {
      this.error = acknowledged ? t('Connection interrupted. Checking the saved answer…', '连接中断，正在检查已保存的回答…') : t('Could not send. Your question is preserved; send again to retry.', '发送失败，问题已保留；可再次发送重试。');
    } finally {
      this.streaming = false; this.controls();
      // Next regular poll reconciles partial/completed answers across viewers.
    }
  }
}

function requestId(): string {
  // crypto.randomUUID is unavailable on the existing HTTP ECS origin.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
}
