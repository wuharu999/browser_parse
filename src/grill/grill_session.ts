import { CustomerAnswer, GrillSession } from './types';
import { GrillReportView } from './grill_report';

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
    this.renderLoading('Connecting to scenario session...');
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
          throw new Error('Session not found or invalid token. Please check your link.');
        }
        throw new Error(`Failed to load session: HTTP ${res.status}`);
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
    const questionLimit = 30;
    const progressPercent = Math.min(100, Math.round((this.session.question_count / questionLimit) * 100));

    header.innerHTML = `
      <div class="session-top-meta">
        <div>
          <span class="grill-badge ${statusBadgeClass}">${this.session.status.replace(/_/g, ' ').toUpperCase()}</span>
          <h1 class="session-intent">${this.escape(this.session.task_intent)}</h1>
          ${this.session.referenced_robot ? `<div class="session-robot">Target Robot: <strong>${this.escape(this.session.referenced_robot)}</strong></div>` : ''}
        </div>
        <div class="session-counter-box">
          <div class="counter-label">Question Budget</div>
          <div class="counter-val">${this.session.question_count} <span class="counter-max">/ ${questionLimit}</span></div>
          <div class="progress-bar-bg">
            <div class="progress-bar-fill" style="width: ${progressPercent}%"></div>
          </div>
        </div>
      </div>
    `;
    wrapper.appendChild(header);

    // Body based on state
    if (this.session.status === 'intake_pending' || (this.session.status === 'interviewing' && (!this.session.active_questions || this.session.active_questions.length === 0))) {
      wrapper.appendChild(this.renderWaitingState('Worker Container Initializing Turn...', 'Codex is reviewing your scenario intent, extracting specs from attached files, and formulating high-impact interview questions.'));
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

  private renderWaitingState(title: string, message: string): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card grill-loading-card';
    card.innerHTML = `
      <div class="grill-spinner"></div>
      <h3 class="loading-title">${this.escape(title)}</h3>
      <p class="loading-desc">${this.escape(message)}</p>
      <div class="loading-note">Running in an isolated sandbox container. Resources will pause between questions.</div>
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
      <h2 class="turn-title">Interview Batch (${questions.length} question${questions.length > 1 ? 's' : ''})</h2>
      <p class="turn-subtitle">Select an option, enter custom specs, or click "Not sure" for each question below.</p>
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
          <span class="question-num">Question ${i + 1}</span>
          ${q.why ? `<span class="question-why">💡 Why: ${this.escape(q.why)}</span>` : ''}
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
        unknownBtn.innerHTML = `<span class="opt-unknown-icon">❓</span> <strong>I don't know / Not sure</strong>`;
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
        freeInput.placeholder = 'Or enter custom specification (Other)...';
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
    submitBtn.textContent = 'Submit Batch Answers  →';
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
        this.showNotice(noticeEl, `Please provide an answer or click "Not sure" for: "${q.text}"`, true);
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
    submitBtn.textContent = 'Submitting answers...';

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
      this.showNotice(noticeEl, `Error submitting answers: ${msg}`, true);
      submitBtn.disabled = false;
      submitBtn.textContent = 'Submit Batch Answers  →';
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
        <span class="grill-badge badge-success">✓ Scenario Ready for Confirmation</span>
        <h2 class="confirm-title">Review Scenario Readback Summary</h2>
        <p class="confirm-subtitle">
          The interview has captured the critical operational boundaries. Please review the summary below.
          Once confirmed, 3 specialist subagents (Capability, Architecture, Risk) will run in parallel to generate the final report.
        </p>
      </div>

      <div class="readback-box">
        <div class="readback-label">Confirmed Scenario Readback:</div>
        <p class="readback-text">${this.escape(summaryText)}</p>
      </div>

      <div class="bt-summary-preview">
        <strong>Modeled Behavior Tree:</strong> ${nodeCount} nodes synthesized (Root: <code>${this.escape(tree?.root_id || 'root')}</code>)
      </div>

      <div class="grill-form-group confirm-notes-group">
        <label class="grill-label" for="confirm-notes">Additional Customer Notes (Optional)</label>
        <textarea id="confirm-notes" class="grill-textarea" rows="3" placeholder="Any final clarifications or emphasis for the specialist agents...">${this.escape(this.confirmationNote)}</textarea>
      </div>

      <div class="confirm-actions">
        <button type="button" class="grill-btn grill-btn-primary btn-confirm-launch" id="btn-confirm">
          Confirm Scenario & Generate Final Report  🚀
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
    confirmBtn.textContent = 'Launching Specialist Subagents...';

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
      this.showNotice(noticeEl, `Error confirming scenario: ${msg}`, true);
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Confirm Scenario & Generate Final Report  🚀';
      this.isSubmitting = false;
    }
  }

  private renderAnalyzingState(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card grill-analyzing-card';
    card.innerHTML = `
      <div class="grill-spinner"></div>
      <h2 class="analyzing-title">Specialist Subagents Evaluating Scenario</h2>
      <p class="analyzing-subtitle">Three specialist analysts are assessing technical feasibility in parallel:</p>

      <div class="specialist-cards-grid">
        <div class="specialist-card">
          <div class="specialist-icon">🤖</div>
          <strong class="specialist-name">Capability Analyst</strong>
          <div class="specialist-desc">Verifying payload, reach, sensor envelope, and hardware constraints.</div>
          <div class="specialist-badge">Running</div>
        </div>

        <div class="specialist-card">
          <div class="specialist-icon">⚙️</div>
          <strong class="specialist-name">Integration Analyst</strong>
          <div class="specialist-desc">Synthesizing ROS 2 packages, node topologies, and topic contracts.</div>
          <div class="specialist-badge">Running</div>
        </div>

        <div class="specialist-card">
          <div class="specialist-icon">🛡️</div>
          <strong class="specialist-name">Risk & Evidence Analyst</strong>
          <div class="specialist-desc">Evaluating failure recovery, safety boundaries, and wiki citations.</div>
          <div class="specialist-badge">Running</div>
        </div>
      </div>
      <div class="analyzing-note">This typically completes in ~15–30 seconds. Your report will appear automatically.</div>
    `;
    return card;
  }

  private renderFailedState(): HTMLElement {
    const card = document.createElement('div');
    card.className = 'grill-card grill-failed-card';
    card.innerHTML = `
      <h2 class="failed-title">⚠️ Session Failed</h2>
      <p class="failed-desc">${this.escape(this.session?.error_message || 'An error occurred during turn processing.')}</p>
      <button type="button" class="grill-btn grill-btn-secondary" id="btn-restart">Start New Interview</button>
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
      <h2 class="failed-title">Access Error</h2>
      <p class="failed-desc">${this.escape(msg)}</p>
      <button type="button" class="grill-btn grill-btn-primary" id="btn-back-grill">Back to Grill Home</button>
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
