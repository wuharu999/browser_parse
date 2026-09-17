import { CustomerAnswer, GrillSession } from './types';
import { GrillReportView } from './grill_report';
import { formatRobotName } from './grill_intake';
import { t } from '../i18n';

export class GrillSessionView {
  private token: string;
  private container: HTMLElement;
  private onNavigate: (path: string) => void;
  private session: GrillSession | null = null;
  private answers: Map<string, CustomerAnswer> = new Map();
  private confirmationNote = '';
  private isSubmitting = false;
  private pollTimeout: number | null = null;
  private isDestroyed = false;

  constructor(token: string, container: HTMLElement, onNavigate: (path: string) => void) {
    this.token = token;
    this.container = container;
    this.onNavigate = onNavigate;
  }

  public destroy(): void {
    this.isDestroyed = true;
    if (this.pollTimeout) {
      window.clearTimeout(this.pollTimeout);
      this.pollTimeout = null;
    }
  }

  public async start(): Promise<void> {
    this.renderLoading(t('Connecting to scenario session...', '正在连接场景推演会话...'));
    await this.fetchSession();
  }

  private schedulePoll(ms = 2000): void {
    if (this.isDestroyed) return;
    if (this.pollTimeout) window.clearTimeout(this.pollTimeout);
    this.pollTimeout = window.setTimeout(async () => {
      if (this.isDestroyed) return;
      await this.fetchSession(true);
    }, ms);
  }

  private async fetchSession(isSilent = false): Promise<void> {
    try {
      const res = await fetch(`/api/grill/session-by-token?token=${encodeURIComponent(this.token)}`);
      if (!res.ok) {
        if (res.status === 404 || res.status === 403) {
          throw new Error(t('Session not found or invalid token. Please check your link.', '推演会话未找到或访问令牌无效，请检查链接。'));
        }
        throw new Error(t(`Failed to load session: HTTP ${res.status}`, `加载会话失败：HTTP ${res.status}`));
      }

      const data: GrillSession = await res.json();
      this.session = data;
      this.render();

      // Determine if we need to continue polling
      const shouldPoll =
        data.status === 'intake_pending' ||
        data.status === 'analyzing' ||
        (data.status === 'interviewing' && (!data.active_questions || data.active_questions.length === 0));

      if (shouldPoll && !this.isDestroyed) {
        this.schedulePoll(2000);
      }
    } catch (err: unknown) {
      if (!isSilent) {
        const msg = err instanceof Error ? err.message : String(err);
        this.renderError(msg);
      }
    }
  }

  public render(): void {
    if (!this.session) return;
    this.container.replaceChildren();

    if (this.session.status === 'completed' && this.session.final_report) {
      const reportView = new GrillReportView(this.session.final_report, this.session, this.container);
      reportView.render();
      return;
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'grill-session-container';

    // Top status banner & progress
    const header = document.createElement('div');
    header.className = 'grill-session-header';

    const statusBadgeClass = `status-badge ${this.session.status}`;
    const questionLimit = 25;
    const progressPercent = Math.min(100, Math.round((this.session.question_count / questionLimit) * 100));

    const statusMap: Record<string, string> = {
      intake_pending: t('Initializing', '初始化中'),
      interviewing: t('Interviewing', '推演访谈中'),
      ready_for_confirmation: t('Ready for Confirmation', '待客户确认'),
      analyzing: t('Analyzing', '专家分析中'),
      completed: t('Completed', '已完成'),
      failed: t('Failed', '失败'),
    };
    const statusText = statusMap[this.session.status] || this.session.status.replace(/_/g, ' ').toUpperCase();

    header.innerHTML = `
      <div class="session-top-meta">
        <div>
          <span class="grill-badge ${statusBadgeClass}">${statusText}</span>
          <h1 class="session-intent">${this.escape(this.session.task_intent)}</h1>
          ${this.session.referenced_robot ? `<div class="session-robot">${t('Target Robot:', '目标机器人：')} <strong>${this.escape(formatRobotName(this.session.referenced_robot))}</strong></div>` : ''}
        </div>
        <div class="session-counter-box">
          <div class="counter-label">${t('Question Budget', '问题配额')}</div>
          <div class="counter-val">${this.session.question_count} <span class="counter-max">/ ${questionLimit}</span></div>
          <div class="progress-bar-bg">
            <div class="progress-bar-fill" style="width: ${progressPercent}%"></div>
          </div>
        </div>
      </div>
    `;
    wrapper.appendChild(header);

    // Phased wind-down guidance hint
    if (this.session.question_count >= 20) {
      const hint = document.createElement('div');
      hint.className = 'budget-hint urgent';
      hint.textContent = t('Final turn budget (at most 5 questions remaining). Preparing final readback.', '推演已接近上限（剩余最多 5 题）。请聚焦未决关键决策并准备最终确认摘要。');
      wrapper.appendChild(hint);
    } else if (this.session.question_count >= 15) {
      const hint = document.createElement('div');
      hint.className = 'budget-hint warning';
      hint.textContent = t('Approaching question limit (at most 10 questions remaining). Focusing on key constraints.', '推演提问已达 15 题（后续最多还可提问 10 题，如信息已充分无需问满）。请聚焦关键约束。');
      wrapper.appendChild(hint);
    }

    // Uploaded Documents card
    if (this.session.files && this.session.files.length > 0) {
      const filesCard = document.createElement('div');
      filesCard.className = 'grill-card session-files-card';
      const h3 = document.createElement('h3');
      h3.textContent = `${t('Uploaded Documents & Diagrams', '上传参考文档与图纸')} (${this.session.files.length})`;
      filesCard.appendChild(h3);

      for (const f of this.session.files) {
        const fileRow = document.createElement('div');
        fileRow.className = 'file-item';
        const fileLink = document.createElement('a');
        fileLink.href = `/api/grill/sessions/${this.session.id}/files/${f.id}?token=${encodeURIComponent(this.token)}`;
        fileLink.target = '_blank';
        fileLink.className = 'file-link';
        fileLink.textContent = `📎 ${f.name} (${f.size} B)`;
        fileRow.appendChild(fileLink);
        filesCard.appendChild(fileRow);
      }
      wrapper.appendChild(filesCard);
    }

    // Past Turn Transcript
    if (this.session.turns && this.session.turns.length > 0) {
      const turnsCard = document.createElement('div');
      turnsCard.className = 'grill-card turns-history-card';
      const h3 = document.createElement('h3');
      h3.textContent = t('Past Turn Transcript', '往轮问答推演记录');
      turnsCard.appendChild(h3);

      for (const turn of this.session.turns) {
        const turnRow = document.createElement('div');
        turnRow.className = 'turn-row';
        turnRow.setAttribute('data-turn-index', String(turn.turn_index));

        const turnHeader = document.createElement('div');
        turnHeader.className = 'turn-row-header';
        turnHeader.textContent = `${t('Turn', '第')} ${turn.turn_index} ${t('', '轮问答')}`;
        turnRow.appendChild(turnHeader);

        for (const q of turn.questions) {
          const qBox = document.createElement('div');
          qBox.className = 'past-question-box';
          qBox.textContent = `Q: ${q.text} ${q.why ? `(💡 ${q.why})` : ''}`;
          turnRow.appendChild(qBox);
        }
        for (const a of turn.answers) {
          const aBox = document.createElement('div');
          aBox.className = 'past-answer-box';
          const ansText = a.selected_option || a.free_text_answer || (a.is_unknown ? 'Unknown' : '');
          aBox.textContent = `A: ${ansText}`;
          turnRow.appendChild(aBox);
        }
        turnsCard.appendChild(turnRow);
      }
      wrapper.appendChild(turnsCard);
    }

    // Body based on state
    if (this.session.status === 'intake_pending' || (this.session.status === 'interviewing' && (!this.session.active_questions || this.session.active_questions.length === 0))) {
      wrapper.appendChild(this.renderWaitingState(
        t('Worker Container Initializing Turn...', '计算容器正在初始化问答轮次...')
      ));
    } else if (this.session.status === 'interviewing') {
      wrapper.appendChild(this.renderInterviewTurn());
    } else if (this.session.status === 'ready_for_confirmation') {
      wrapper.appendChild(this.renderConfirmationState());
    } else if (this.session.status === 'analyzing') {
      wrapper.appendChild(this.renderAnalyzingState());
    } else if (this.session.status === 'failed') {
      wrapper.appendChild(this.renderFailedState());
    }

    this.container.appendChild(wrapper);
  }

  private renderWaitingState(title: string, message?: string): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card grill-loading-card';
    const displayTitle = (this.session?.container_state === 'hibernated' || !title)
      ? t('Warming up container and resuming session...', '正在唤醒计算容器并恢复推演会话...')
      : title;
    card.innerHTML = `
      <div class="grill-spinner"></div>
      <h3 class="loading-title">${this.escape(displayTitle)}</h3>
      ${message ? `<p class="loading-desc">${this.escape(message)}</p>` : ''}
      <div class="loading-note">${t('Running in an isolated container. Resources will pause between questions.', '运行于隔离计算容器中。问题生成间隙资源将自动暂停以节省算力。')}</div>
    `;
    return card;
  }

  private renderInterviewTurn(): HTMLElement {
    const turnCard = document.createElement('div');
    turnCard.className = 'grill-turn-card';

    const noticeEl = document.createElement('div');
    noticeEl.className = 'grill-notice';
    noticeEl.style.display = 'none';
    turnCard.appendChild(noticeEl);

    const questions = this.session?.active_questions || [];
    const turnIntro = document.createElement('div');
    turnIntro.className = 'turn-intro';
    turnIntro.innerHTML = `
      <h2 class="turn-title">${t(`Interview Batch (${questions.length} question${questions.length > 1 ? 's' : ''})`, `本轮访谈问答（共 ${questions.length} 题）`)}</h2>
      <p class="turn-subtitle">${t('Select an option, enter custom specs, or click "Not sure" for each question below.', '请为以下问题选择一个选项、输入自定义规格，或点击“我不确定”。')}</p>
    `;
    turnCard.appendChild(turnIntro);

    // Render each question card
    for (let i = 0; i < questions.length; i++) {
      const q = questions[i];
      const qCard = document.createElement('div');
      qCard.className = 'question-card';

      // Current answer state for this question
      let currentAns = this.answers.get(q.id);
      if (!currentAns) {
        currentAns = { question_id: q.id, selected_option: null, free_text_answer: null, is_unknown: false };
        this.answers.set(q.id, currentAns);
      }

      qCard.innerHTML = `
        <div class="question-header">
          <span class="question-num">${t('Question', '问题')} ${i + 1}</span>
          ${q.why ? `<span class="question-why">💡 ${t('Why:', '提问原因：')} ${this.escape(q.why)}</span>` : ''}
        </div>
        <div class="question-text">${this.escape(q.text)}</div>
      `;

      // Options container
      const optionsBox = document.createElement('div');
      optionsBox.className = 'options-box';

      // 3 Suggested Options
      for (const opt of q.options) {
        const optBtn = document.createElement('button');
        optBtn.type = 'button';
        const isSelected = currentAns.selected_option === opt.label && !currentAns.is_unknown && !currentAns.free_text_answer;
        optBtn.className = `option-pill ${isSelected ? 'selected' : ''}`;
        optBtn.innerHTML = `
          <strong class="opt-label">${this.escape(opt.label)}</strong>
          ${opt.interpretation ? `<span class="opt-interp">${this.escape(opt.interpretation)}</span>` : ''}
        `;
        optBtn.addEventListener('click', () => {
          this.answers.set(q.id, {
            question_id: q.id,
            selected_option: opt.label,
            free_text_answer: null,
            is_unknown: false,
          });
          this.render();
        });
        optionsBox.appendChild(optBtn);
      }

      // "I don't know / Not sure" button
      if (q.allow_unknown !== false) {
        const unknownBtn = document.createElement('button');
        unknownBtn.type = 'button';
        const isUnknown = !!currentAns.is_unknown;
        unknownBtn.className = `option-pill option-unknown ${isUnknown ? 'selected' : ''}`;
        unknownBtn.innerHTML = `<span class="opt-unknown-icon">❓</span> <strong>${t("I don't know / Not sure", '我不确定 / 暂无规定')}</strong>`;
        unknownBtn.addEventListener('click', () => {
          this.answers.set(q.id, {
            question_id: q.id,
            selected_option: null,
            free_text_answer: null,
            is_unknown: true,
          });
          this.render();
        });
        optionsBox.appendChild(unknownBtn);
      }

      qCard.appendChild(optionsBox);

      // Free-text input
      if (q.free_text !== false) {
        const freeTextBox = document.createElement('div');
        freeTextBox.className = 'free-text-box';
        const freeInput = document.createElement('input');
        freeInput.type = 'text';
        freeInput.className = 'grill-input free-text-input';
        freeInput.placeholder = t('Or enter custom specification (Other)...', '或输入自定义规格（其他）...');
        freeInput.value = currentAns.free_text_answer || '';

        freeInput.addEventListener('input', () => {
          const val = freeInput.value.trim();
          if (val) {
            this.answers.set(q.id, {
              question_id: q.id,
              selected_option: null,
              free_text_answer: val,
              is_unknown: false,
            });
          } else if (currentAns?.free_text_answer) {
            this.answers.set(q.id, {
              question_id: q.id,
              selected_option: null,
              free_text_answer: null,
              is_unknown: false,
            });
          }
        });

        freeTextBox.appendChild(freeInput);
        qCard.appendChild(freeTextBox);
      }

      turnCard.appendChild(qCard);
    }

    // Submit Turn button
    const actions = document.createElement('div');
    actions.className = 'grill-turn-actions';

    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'grill-btn grill-btn-primary';
    submitBtn.textContent = t('Submit Batch Answers  →', '提交本轮回答  →');
    submitBtn.addEventListener('click', () => this.submitTurn(noticeEl, submitBtn));

    actions.appendChild(submitBtn);
    turnCard.appendChild(actions);

    return turnCard;
  }

  private async submitTurn(noticeEl: HTMLElement, submitBtn: HTMLButtonElement): Promise<void> {
    if (this.isSubmitting) return;

    // Validate that all questions have an answer
    const questions = this.session?.active_questions || [];
    const submissionAnswers: CustomerAnswer[] = [];

    for (const q of questions) {
      const ans = this.answers.get(q.id);
      const hasOption = !!ans?.selected_option;
      const hasFree = !!ans?.free_text_answer?.trim();
      const hasUnknown = !!ans?.is_unknown;

      if (!hasOption && !hasFree && !hasUnknown) {
        this.showNotice(noticeEl, t(`Please provide an answer or click "Not sure" for: "${q.text}"`, `请为以下问题提供答案或点击“我不确定”：“${q.text}”`), true);
        return;
      }

      submissionAnswers.push({
        question_id: q.id,
        selected_option: (hasOption && ans) ? ans.selected_option : null,
        free_text_answer: (hasFree && ans) ? ans.free_text_answer : null,
        is_unknown: hasUnknown,
      });
    }

    this.isSubmitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = t('Submitting answers...', '正在提交回答...');

    try {
      const res = await fetch(`/api/grill/sessions/${this.session?.id}/turns`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${this.token}`,
        },
        body: JSON.stringify({ answers: submissionAnswers }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || `Submission failed: HTTP ${res.status}`);
      }

      const updated: GrillSession = await res.json();
      this.session = updated;
      this.answers.clear();
      this.isSubmitting = false;
      this.render();
      this.schedulePoll(2000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.showNotice(noticeEl, t(`Error submitting answers: ${msg}`, `提交回答失败：${msg}`), true);
      submitBtn.disabled = false;
      submitBtn.textContent = t('Submit Batch Answers  →', '提交本轮回答  →');
      this.isSubmitting = false;
    }
  }

  private renderConfirmationState(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card confirmation-card';

    const noticeEl = document.createElement('div');
    noticeEl.className = 'grill-notice';
    noticeEl.style.display = 'none';
    card.appendChild(noticeEl);

    const summaryText = this.session?.readback_summary || this.session?.task_intent || '';
    const tree = this.session?.scenario_state;
    const nodeCount = tree?.nodes?.length || 0;

    card.innerHTML += `
      <div class="confirmation-header">
        <span class="grill-badge badge-success">${t('✓ Scenario Ready for Confirmation', '✓ 场景就绪，等待确认')}</span>
        <h2 class="confirm-title">${t('Review Scenario Readback Summary', '审阅场景确认摘要（Readback）')}</h2>
        <p class="confirm-subtitle">
          ${t(
            'The interview has captured the critical operational boundaries. Please review the summary below. Once confirmed, 3 specialist subagents (Capability, Architecture, Risk) will run in parallel to generate the final report.',
            '访谈已充分捕捉关键运行边界与约束。请审阅下方摘要。确认后，3位专家子智能体（硬件能力、系统架构、运行风险）将并行推演以生成最终评估报告。'
          )}
        </p>
      </div>

      <div class="readback-box">
        <div class="readback-label">${t('Confirmed Scenario Readback:', '已确认场景摘要：')}</div>
        <p class="readback-text">${this.escape(summaryText)}</p>
      </div>

      <div class="bt-summary-preview">
        <strong>${t('Modeled Behavior Tree:', '建模行为树：')}</strong> ${nodeCount} ${t('nodes synthesized', '个节点已合成')} (Root: <code>${this.escape(tree?.root_id || 'root')}</code>)
      </div>

      <div class="grill-form-group confirm-notes-group">
        <label class="grill-label" for="confirm-notes">${t('Additional Customer Notes (Optional)', '补充说明 / 客户批注（可选）')}</label>
        <textarea id="confirm-notes" class="grill-textarea" rows="3" placeholder="${t('Any final clarifications or emphasis for the specialist agents...', '可输入对专家智能体的补充说明、特别侧重点或约束要求...')}">${this.escape(this.confirmationNote)}</textarea>
      </div>

      <div class="confirm-actions">
        <button type="button" class="grill-btn grill-btn-primary btn-confirm-launch" id="btn-confirm">
          ${t('Confirm Scenario & Generate Final Report  🚀', '确认场景并启动专家综合推演  🚀')}
        </button>
      </div>
    `;

    const notesArea = card.querySelector('#confirm-notes') as HTMLTextAreaElement;
    notesArea?.addEventListener('input', () => {
      this.confirmationNote = notesArea.value;
    });

    const confirmBtn = card.querySelector('#btn-confirm') as HTMLButtonElement;
    confirmBtn?.addEventListener('click', () => this.confirmScenario(noticeEl, confirmBtn));

    return card;
  }

  private async confirmScenario(noticeEl: HTMLElement, confirmBtn: HTMLButtonElement): Promise<void> {
    if (this.isSubmitting) return;
    this.isSubmitting = true;
    confirmBtn.disabled = true;
    confirmBtn.textContent = t('Launching Specialist Subagents...', '正在启动专家子智能体...');

    try {
      const res = await fetch(`/api/grill/sessions/${this.session?.id}/confirm`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${this.token}`,
        },
        body: JSON.stringify({ confirmation_note: this.confirmationNote }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Confirmation failed: HTTP ${res.status}`);
      }

      const updated: GrillSession = await res.json();
      this.session = updated;
      this.isSubmitting = false;
      this.render();
      this.schedulePoll(2000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.showNotice(noticeEl, t(`Error confirming scenario: ${msg}`, `确认场景失败：${msg}`), true);
      confirmBtn.disabled = false;
      confirmBtn.textContent = t('Confirm Scenario & Generate Final Report  🚀', '确认场景并启动专家综合推演  🚀');
      this.isSubmitting = false;
    }
  }

  private renderAnalyzingState(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card grill-analyzing-card';
    card.innerHTML = `
      <div class="grill-spinner"></div>
      <h2 class="analyzing-title">${t('Specialist Subagents Evaluating Scenario', '专家子智能体正在评估场景')}</h2>
      <p class="analyzing-subtitle">${t('Three specialist analysts are assessing technical feasibility in parallel:', '三位专业分析师正并行评估技术可行性：')}</p>

      <div class="specialist-cards-grid">
        <div class="specialist-card">
          <div class="specialist-icon">🤖</div>
          <strong class="specialist-name">${t('Capability Analyst', '硬件能力分析师')}</strong>
          <div class="specialist-desc">${t('Verifying payload, reach, sensor envelope, and hardware constraints.', '验证有效负载、机械臂工作半径、传感器包络及硬件物理约束。')}</div>
          <div class="specialist-badge">${t('Running', '推演中')}</div>
        </div>

        <div class="specialist-card">
          <div class="specialist-icon">⚙️</div>
          <strong class="specialist-name">${t('Integration Analyst', '系统集成分析师')}</strong>
          <div class="specialist-desc">${t('Synthesizing ROS 2 packages, node topologies, and topic contracts.', '生成 ROS 2 软件包拓扑、节点通信契约与消息管道。')}</div>
          <div class="specialist-badge">${t('Running', '推演中')}</div>
        </div>

        <div class="specialist-card">
          <div class="specialist-icon">🛡️</div>
          <strong class="specialist-name">${t('Risk & Evidence Analyst', '风险与证据分析师')}</strong>
          <div class="specialist-desc">${t('Evaluating failure recovery, safety boundaries, and wiki citations.', '评估故障自愈机制、安全运行边界与知识库引证。')}</div>
          <div class="specialist-badge">${t('Running', '推演中')}</div>
        </div>
      </div>
      <div class="analyzing-note">${t('This typically completes in ~15–30 seconds. Your report will appear automatically.', '专家综合推演通常耗时约 15–30 秒。评估报告生成后将自动展示。')}</div>
    `;
    return card;
  }

  private renderFailedState(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card grill-failed-card';
    card.innerHTML = `
      <h2 class="failed-title">${t('⚠️ Session Failed', '⚠️ 会话处理失败')}</h2>
      <p class="failed-desc">${this.escape(this.session?.error_message || t('An error occurred during turn processing.', '轮次处理过程中发生错误。'))}</p>
      <button type="button" class="grill-btn grill-btn-secondary" id="btn-restart">${t('Start New Interview', '发起新访谈')}</button>
    `;
    const restartBtn = card.querySelector('#btn-restart');
    restartBtn?.addEventListener('click', () => this.onNavigate('/grill'));
    return card;
  }

  private renderLoading(msg: string): void {
    this.container.replaceChildren();
    const card = document.createElement('div');
    card.className = 'grill-card grill-loading-card';
    card.innerHTML = `<div class="grill-spinner"></div><p>${this.escape(msg)}</p>`;
    this.container.appendChild(card);
  }

  private renderError(msg: string): void {
    this.container.replaceChildren();
    const card = document.createElement('div');
    card.className = 'grill-card grill-failed-card';
    card.innerHTML = `
      <h2 class="failed-title">${t('Access Error', '访问错误')}</h2>
      <p class="failed-desc">${this.escape(msg)}</p>
      <button type="button" class="grill-btn grill-btn-primary" id="btn-back-grill">${t('Back to Grill Home', '返回推演首页')}</button>
    `;
    const btn = card.querySelector('#btn-back-grill');
    btn?.addEventListener('click', () => this.onNavigate('/grill'));
    this.container.appendChild(card);
  }

  private showNotice(el: HTMLElement, msg: string, isError = false): void {
    el.textContent = msg;
    el.className = `grill-notice ${isError ? 'grill-notice-error' : 'grill-notice-info'}`;
    el.style.display = 'block';
  }

  private escape(str: string): string {
    const p = document.createElement('p');
    p.textContent = str;
    return p.innerHTML;
  }
}
