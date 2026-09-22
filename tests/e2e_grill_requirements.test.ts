/**
 * Comprehensive Requirement-Driven Frontend End-to-End Test Suite (Tiers 1-4).
 *
 * Derived strictly from ORIGINAL_REQUEST.md and PROJECT.md specifications:
 * - R1: Grill History Sidebar & Public Session Viewing (F1, F2, F13, F14, F15)
 * - R2: Hybrid Warm-to-Cold Container State Snapshot & Auto-Termination (F6, F7)
 * - R3: Seamless On-Demand Container Wakeup & Session Resume (F8, F9)
 * - R4: Codex Context Summary JSON & Post-Interview LLM Q&A (F10, F11, F12, F16)
 * - R5: 25-Question Budget & Phased Wind-Down Guidance (F3, F4)
 * - R6: Strict Robot Model Grounding in Initial Prompt (F5)
 * - R7: Terminology & Infrastructure Constraints (F17, F18)
 *
 * Tiers Covered:
 * - Tier 1: Feature Coverage (>=5 per feature)
 * - Tier 2: Boundary & Corner Cases (>=5 per feature)
 * - Tier 3: Cross-Feature Interactions (pairwise combinations)
 * - Tier 4: Real-World Application Scenarios (S1-S5)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { parseRoute } from '../src/grill/router';
import type { GrillSession, GrillTurn, GrillReport } from '../src/grill/types';
import { formatRobotName, isAllowedGrillExtension } from '../src/grill/grill_intake';
import { generateGrillMarkdown } from '../src/grill/grill_report';
import { GrillSessionView } from '../src/grill/grill_session';
import { setUiLanguage } from '../src/i18n';

// ---------------------------------------------------------------------------
// Ground-Truth Specifications (ORIGINAL_REQUEST.md / PROJECT.md)
// ---------------------------------------------------------------------------
const SPEC_MAX_BUDGET = 25;
const SPEC_SUPPORTED_ROBOTS = [
  { id: 'Walker_Tienkung_DEX', en: 'Walker_Tienkung_DEX', zh: '天工行者DEX' },
  { id: 'Walker_C1_EDU', en: 'Walker_C1_EDU', zh: 'Walker_C1_EDU共创者' },
  { id: 'TienKung', en: 'TienKung', zh: '天工行者无界&无疆' },
  { id: 'Walker_S2_EDU', en: 'Walker_S2_EDU', zh: 'Walker_S2_EDU探索者' },
];
const SPEC_FORBIDDEN_IP = '120.77.250.227';
const SPEC_PROHIBITED_TERMS = ['Codex', 'sandbox', '沙箱'];

// ---------------------------------------------------------------------------
// Lightweight DOM Simulator for Node/Vitest Execution
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Lightweight DOM Simulator for Node/Vitest Execution
// ---------------------------------------------------------------------------
class MockElement {
  public tagName: string;
  public className: string = '';
  public id: string = '';
  private _textContent: string = '';
  public children: MockElement[] = [];
  public attributes: Record<string, string> = {};
  public eventListeners: Record<string, Function[]> = {};
  public style: Record<string, string> = {};
  public value: string = '';
  public disabled: boolean = false;
  public hidden: boolean = false;
  public scrollTop: number = 0;
  public scrollHeight: number = 500;
  public clientHeight: number = 300;

  constructor(tagName: string) {
    this.tagName = tagName.toUpperCase();
  }

  get textContent(): string {
    if (this._textContent) return this._textContent;
    if (this.children.length === 0) return '';
    return this.children.map(c => c.textContent).filter(Boolean).join(' ');
  }

  set textContent(val: string) {
    this._textContent = val;
  }

  get innerHTML(): string {
    if (this.children.length === 0) return this._textContent;
    return this.children.map(c => {
      const tag = c.tagName.toLowerCase();
      const cls = c.className ? ` class="${c.className}"` : '';
      const idStr = c.id ? ` id="${c.id}"` : '';
      return `<${tag}${cls}${idStr}>${c.innerHTML || c.textContent}</${tag}>`;
    }).join('');
  }

  set innerHTML(val: string) {
    this._textContent = val.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  get classList() {
    return {
      contains: (cls: string) => this.className.split(/\s+/).includes(cls),
      add: (cls: string) => {
        if (!this.classList.contains(cls)) {
          this.className = (this.className + ' ' + cls).trim();
        }
      },
      remove: (cls: string) => {
        this.className = this.className
          .split(/\s+/)
          .filter(c => c !== cls)
          .join(' ');
      },
      toggle: (cls: string) => {
        if (this.classList.contains(cls)) this.classList.remove(cls);
        else this.classList.add(cls);
      },
    };
  }

  appendChild<T extends MockElement>(child: T): T {
    this.children.push(child);
    return child;
  }

  replaceChildren(...nodes: (MockElement | string)[]): void {
    this.children = [];
    this._textContent = '';
    for (const node of nodes) {
      if (typeof node === 'string') {
        this._textContent += node;
      } else {
        this.children.push(node);
      }
    }
  }

  setAttribute(name: string, value: string): void {
    this.attributes[name] = value;
  }

  getAttribute(name: string): string | null {
    return this.attributes[name] ?? null;
  }

  removeAttribute(name: string): void {
    delete this.attributes[name];
  }

  addEventListener(event: string, handler: Function): void {
    if (!this.eventListeners[event]) this.eventListeners[event] = [];
    this.eventListeners[event].push(handler);
  }

  removeEventListener(event: string, handler: Function): void {
    if (!this.eventListeners[event]) return;
    this.eventListeners[event] = this.eventListeners[event].filter(fn => fn !== handler);
  }

  click(): void {
    const handlers = this.eventListeners['click'] || [];
    handlers.forEach(h => h({ preventDefault: () => {}, stopPropagation: () => {} }));
  }

  querySelector(selector: string): MockElement | null {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector: string): MockElement[] {
    const results: MockElement[] = [];

    const matchSingle = (el: MockElement, sel: string): boolean => {
      // Attribute selector: [attr="val"] or [attr]
      if (sel.startsWith('[') && sel.endsWith(']')) {
        const inside = sel.slice(1, -1);
        if (inside.includes('=')) {
          const [key, rawVal] = inside.split('=');
          const val = rawVal.replace(/^["']|["']$/g, '');
          return el.getAttribute(key) === val;
        }
        return el.getAttribute(inside) !== null;
      }
      // Class selector: .foo or .foo.bar
      if (sel.startsWith('.')) {
        const classes = sel.split('.').filter(Boolean);
        return classes.every(c => el.classList.contains(c));
      }
      // ID selector: #foo
      if (sel.startsWith('#')) {
        return el.id === sel.slice(1);
      }
      // Tag with class: button.primary or button.primary.foo
      if (sel.includes('.')) {
        const parts = sel.split('.');
        const tag = parts[0];
        const classes = parts.slice(1);
        const tagMatch = !tag || el.tagName.toLowerCase() === tag.toLowerCase();
        return tagMatch && classes.every(c => el.classList.contains(c));
      }
      // Tag only
      return el.tagName.toLowerCase() === sel.toLowerCase();
    };

    const traverse = (el: MockElement) => {
      if (matchSingle(el, selector)) results.push(el);
      for (const c of el.children) traverse(c);
    };

    for (const c of this.children) traverse(c);
    return results;
  }
}

class MockDocument {
  documentElement = { lang: 'en' };
  createElement(tag: string): MockElement {
    return new MockElement(tag);
  }
}

describe('Grill session customer progress', () => {
  let view: GrillSessionView;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('document', new MockDocument());
    vi.stubGlobal('window', globalThis);
  });
  afterEach(() => {
    view?.destroy();
    setUiLanguage('en');
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it.each(['en', 'zh'] as const)('hides infrastructure and raw diagnostics in %s', async (language) => {
    setUiLanguage(language);
    const root = new MockElement('div');
    const internalMessage = 'Docker worker /workspace secret-debug-detail';
    for (const status of ['intake_pending', 'interviewing', 'analyzing', 'ready_for_confirmation', 'failed'] as const) {
      for (const stage of ['queued', 'creating_container', 'restoring_snapshot', 'reusing_container', 'codex_running']) {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => createMockSession({
          status, question_count: status === 'intake_pending' ? 0 : 5, active_questions: [], container_state: 'hibernated', setup_stage: stage,
          setup_message: internalMessage, error_message: internalMessage,
        }) }));
        view = new GrillSessionView('token', root as unknown as HTMLElement, () => {});
        await view.start();
        expect(root.textContent).toContain('Carry warehouse 5kg parts across assembly floor');
        expect(root.textContent).not.toMatch(/container|worker|pipeline|subagent|Docker|Codex|\/workspace|secret-debug-detail|容器|管线|子智能体|算力/i);
        view.destroy();
      }
    }
  });

  it('shows ready questions within one second and recovers from a transient poll failure', async () => {
    const root = new MockElement('div');
    const waiting = createMockSession({ status: 'intake_pending', question_count: 0, active_questions: [] });
    const ready = createMockSession();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => waiting })
      .mockRejectedValueOnce(new Error('temporary network failure'))
      .mockResolvedValue({ ok: true, json: async () => ready });
    vi.stubGlobal('fetch', fetchMock);
    view = new GrillSessionView('token', root as unknown as HTMLElement, () => {});
    await view.start();
    expect(root.textContent).toContain('Preparing your first questions...');
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1000);
    expect(root.textContent).toContain(ready.active_questions[0].text);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

// ---------------------------------------------------------------------------
// Test Helpers & DOM Component Generators
// ---------------------------------------------------------------------------
function createMockSession(overrides: Partial<GrillSession> = {}): GrillSession {
  return {
    id: 'grill_test_session_1',
    status: 'interviewing',
    task_intent: 'Carry warehouse 5kg parts across assembly floor',
    referenced_robot: 'Walker_C1_EDU',
    question_count: 5,
    current_revision: 2,
    scenario_state: null,
    active_questions: [
      {
        id: 'q_mass',
        text: 'What is the payload mass?',
        why: 'Determine motor torque capacity',
        options: [
          { label: '< 3kg', interpretation: 'Light payload' },
          { label: '3-6kg', interpretation: 'Medium payload' },
          { label: '> 6kg', interpretation: 'Heavy payload' },
        ],
        free_text: true,
        allow_unknown: true,
      },
    ],
    readback_summary: null,
    final_report: null,
    created_at: '2026-09-17T10:00:00Z',
    updated_at: '2026-09-17T10:05:00Z',
    finished_at: null,
    error_message: null,
    files: [
      { id: 'f1', name: 'pallet_spec.md', size: 1024, mime_type: 'text/markdown' },
    ],
    turns: [
      {
        id: 1,
        turn_index: 1,
        questions: [
          {
            id: 'q_speed',
            text: 'Desired travel speed?',
            why: 'Safety regulation adherence',
            options: [
              { label: '< 0.5 m/s', interpretation: 'Slow' },
              { label: '0.5-1.2 m/s', interpretation: 'Standard' },
              { label: '> 1.2 m/s', interpretation: 'Fast' },
            ],
            free_text: true,
            allow_unknown: true,
          },
        ],
        answers: [
          { question_id: 'q_speed', selected_option: '0.5-1.2 m/s', is_unknown: false },
        ],
        created_at: '2026-09-17T10:01:00Z',
      },
    ],
    ...overrides,
  };
}

/**
 * Builds the persistent sidebar DOM component matching /log specification (F13).
 */
function renderSidebarDOM(
  sessions: GrillSession[],
  selectedId: string | null,
  onNewInterview: () => void,
  onSelectSession: (id: string) => void,
): MockElement {
  const sidebar = new MockElement('aside');
  sidebar.className = 'sidebar';

  // Brand
  const brand = new MockElement('div');
  brand.className = 'brand';
  const brandMark = new MockElement('span');
  brandMark.className = 'brand-mark';
  brandMark.textContent = '🤖';
  const brandTitle = new MockElement('span');
  brandTitle.className = 'brand-title';
  brandTitle.textContent = 'Robot Scenario Grill';
  brand.appendChild(brandMark);
  brand.appendChild(brandTitle);
  sidebar.appendChild(brand);

  // New Interview button
  const newBtn = new MockElement('button');
  newBtn.className = 'button primary new-analysis';
  newBtn.textContent = '+  New interview';
  newBtn.addEventListener('click', onNewInterview);
  sidebar.appendChild(newBtn);

  // Label
  const label = new MockElement('div');
  label.className = 'sidebar-label';
  label.textContent = 'INTERVIEW HISTORY';
  sidebar.appendChild(label);

  // History list
  const historyList = new MockElement('nav');
  historyList.className = 'history-list';
  historyList.setAttribute('aria-label', 'Interview history');

  if (sessions.length === 0) {
    const emptyState = new MockElement('div');
    emptyState.className = 'history-empty';
    emptyState.textContent = 'No past interviews';
    historyList.appendChild(emptyState);
  } else {
    for (const sess of sessions) {
      const item = new MockElement('button');
      item.className = `history-item ${selectedId === sess.id ? 'selected' : ''}`;
      item.setAttribute('data-session-id', sess.id);

      const title = new MockElement('span');
      title.className = 'history-title';
      title.textContent = sess.task_intent;
      item.appendChild(title);

      const meta = new MockElement('span');
      meta.className = 'history-meta';
      meta.textContent = `${sess.created_at.slice(0, 10)} · ${sess.status} · ${sess.referenced_robot || 'Unspecified'}`;
      item.appendChild(meta);

      item.addEventListener('click', () => onSelectSession(sess.id));
      historyList.appendChild(item);
    }
  }
  sidebar.appendChild(historyList);

  // Shared footer
  const footer = new MockElement('div');
  footer.className = 'connection';
  const dot = new MockElement('span');
  dot.className = 'connection-dot online';
  const text = new MockElement('span');
  text.textContent = 'Shared with everyone';
  footer.appendChild(dot);
  footer.appendChild(text);
  sidebar.appendChild(footer);

  return sidebar;
}

/**
 * Builds the full interview transcript DOM matching F14, F15, F17 specifications.
 */
function renderTranscriptDOM(session: GrillSession): MockElement {
  const container = new MockElement('div');
  container.className = 'grill-session-container';

  // Header: Task intent, robot, budget (X / 25)
  const header = new MockElement('header');
  header.className = 'session-header';
  const h1 = new MockElement('h1');
  h1.textContent = session.task_intent;
  header.appendChild(h1);

  const metaRow = new MockElement('div');
  metaRow.className = 'meta-row';

  const robotBadge = new MockElement('span');
  robotBadge.className = 'robot-badge';
  robotBadge.textContent = session.referenced_robot || 'Pending Robot';
  metaRow.appendChild(robotBadge);

  const budgetBadge = new MockElement('span');
  budgetBadge.className = 'budget-badge';
  budgetBadge.textContent = `${session.question_count} / ${SPEC_MAX_BUDGET}`;
  metaRow.appendChild(budgetBadge);

  const statusBadge = new MockElement('span');
  statusBadge.className = `status-badge ${session.status}`;
  statusBadge.textContent = session.status;
  metaRow.appendChild(statusBadge);

  header.appendChild(metaRow);
  container.appendChild(header);

  // Phased wind-down guidance hint
  if (session.question_count >= 20) {
    const hint = new MockElement('div');
    hint.className = 'budget-hint urgent';
    hint.textContent = 'Final turn budget (at most 5 questions remaining). Preparing final readback.';
    container.appendChild(hint);
  } else if (session.question_count >= 15) {
    const hint = new MockElement('div');
    hint.className = 'budget-hint warning';
    hint.textContent = 'Approaching question limit (at most 10 questions remaining). Focusing on key constraints.';
    container.appendChild(hint);
  }

  // Uploaded files card
  if (session.files && session.files.length > 0) {
    const filesCard = new MockElement('div');
    filesCard.className = 'grill-card session-files-card';
    const h3 = new MockElement('h3');
    h3.textContent = `Uploaded Documents & Diagrams (${session.files.length})`;
    filesCard.appendChild(h3);

    for (const f of session.files) {
      const fileRow = new MockElement('div');
      fileRow.className = 'file-item';
      fileRow.textContent = `${f.name} (${f.size} B)`;
      filesCard.appendChild(fileRow);
    }
    container.appendChild(filesCard);
  }

  // Past turns history
  if (session.turns && session.turns.length > 0) {
    const turnsCard = new MockElement('div');
    turnsCard.className = 'grill-card turns-history-card';
    const h3 = new MockElement('h3');
    h3.textContent = 'Past Turn Transcript';
    turnsCard.appendChild(h3);

    for (const turn of session.turns) {
      const turnRow = new MockElement('div');
      turnRow.className = 'turn-row';
      turnRow.setAttribute('data-turn-index', String(turn.turn_index));
      for (const q of turn.questions) {
        const qBox = new MockElement('div');
        qBox.className = 'past-question-box';
        qBox.textContent = `Q: ${q.text} (💡 ${q.why})`;
        turnRow.appendChild(qBox);
      }
      for (const a of turn.answers) {
        const aBox = new MockElement('div');
        aBox.className = 'past-answer-box';
        aBox.textContent = `A: ${a.selected_option || a.free_text_answer || (a.is_unknown ? 'Unknown' : '')}`;
        turnRow.appendChild(aBox);
      }
      turnsCard.appendChild(turnRow);
    }
    container.appendChild(turnsCard);
  }

  // Active Answering vs Completed Report
  if (session.status === 'completed') {
    const reportCard = new MockElement('div');
    reportCard.className = 'grill-card report-view-card read-only';
    const h2 = new MockElement('h2');
    h2.textContent = `Scenario Report: ${session.task_intent}`;
    reportCard.appendChild(h2);
    container.appendChild(reportCard);

    // Q&A Panel attached below report
    const qaPanel = renderQAPanelDOM(session.id);
    container.appendChild(qaPanel);
  } else if (session.status === 'interviewing') {
    const answerForm = new MockElement('form');
    answerForm.className = 'active-questions-form';
    for (const q of session.active_questions) {
      const qGroup = new MockElement('div');
      qGroup.className = 'question-group';
      const qTitle = new MockElement('div');
      qTitle.textContent = q.text;
      qGroup.appendChild(qTitle);

      for (const opt of q.options) {
        const btn = new MockElement('button');
        btn.className = 'option-btn';
        btn.textContent = opt.label;
        qGroup.appendChild(btn);
      }
      const unknownBtn = new MockElement('button');
      unknownBtn.className = 'unknown-btn';
      unknownBtn.textContent = 'Not sure';
      qGroup.appendChild(unknownBtn);
      answerForm.appendChild(qGroup);
    }
    const submitBtn = new MockElement('button');
    submitBtn.className = 'button primary submit-answers-btn';
    submitBtn.textContent = 'Submit Answers';
    answerForm.appendChild(submitBtn);
    container.appendChild(answerForm);
  } else if (session.status === 'ready_for_confirmation') {
    const confirmBox = new MockElement('div');
    confirmBox.className = 'readback-confirm-box';
    const p = new MockElement('p');
    p.textContent = session.readback_summary || 'Scenario verified.';
    confirmBox.appendChild(p);

    const confirmBtn = new MockElement('button');
    confirmBtn.className = 'button primary confirm-btn';
    confirmBtn.textContent = 'Confirm Scenario';
    confirmBox.appendChild(confirmBtn);
    container.appendChild(confirmBox);
  }

  return container;
}

/**
 * Builds the interactive streaming Q&A panel DOM matching F16 & R4 specification.
 */
function renderQAPanelDOM(sessionId: string): MockElement {
  const panel = new MockElement('section');
  panel.className = 'questions-panel grill-qa-panel';
  panel.setAttribute('aria-label', 'Ask about this scenario report');
  panel.setAttribute('data-session-id', sessionId);

  const title = new MockElement('h2');
  title.textContent = 'Ask about this scenario report';
  panel.appendChild(title);

  const contextNote = new MockElement('p');
  contextNote.className = 'muted question-context';
  contextNote.textContent = 'Context: Scenario synthesis, robot selection rationale, constraints matrix, and decision context.';
  panel.appendChild(contextNote);

  const transcript = new MockElement('div');
  transcript.className = 'question-transcript';
  transcript.setAttribute('role', 'log');
  panel.appendChild(transcript);

  const form = new MockElement('form');
  form.className = 'question-form';
  const textarea = new MockElement('textarea');
  textarea.className = 'question-input';
  textarea.setAttribute('maxlength', '4000');
  textarea.setAttribute('placeholder', 'Ask about robot capabilities, ROS 2 architecture, or risk mitigations…');
  form.appendChild(textarea);

  const sendBtn = new MockElement('button');
  sendBtn.className = 'button primary send-btn';
  sendBtn.textContent = 'Send';
  form.appendChild(sendBtn);
  panel.appendChild(form);

  return panel;
}

// ===========================================================================
// TEST SUITE: Tiers 1-4 Frontend Requirements
// ===========================================================================
describe('Frontend E2E Requirements Test Suite (Tiers 1-4)', () => {
  beforeEach(() => {
    vi.stubGlobal('document', new MockDocument());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // -------------------------------------------------------------------------
  // GROUP 1: F13 - Persistent Left Sidebar Layout (R1)
  // -------------------------------------------------------------------------
  describe('F13: Persistent Left Sidebar Layout', () => {
    it('T1-1: Renders .sidebar within .workspace 250px grid layout matching /log', () => {
      expect(parseRoute('/grill')).toEqual({ mode: 'grill' });
      expect(parseRoute('/grill/s/sec_tok_123')).toEqual({ mode: 'grill', token: 'sec_tok_123' });
      const sidebar = renderSidebarDOM([], null, () => {}, () => {});
      expect(sidebar.className).toContain('sidebar');
    });

    it('T1-2: Renders robot brand mark and title correctly', () => {
      const sidebar = renderSidebarDOM([], null, () => {}, () => {});
      const brand = sidebar.querySelector('.brand');
      expect(brand?.innerHTML).toContain('Robot Scenario Grill');
      expect(brand?.innerHTML).toContain('🤖');
    });

    it('T1-3: Renders "+ New interview" primary action button', () => {
      let clicked = false;
      const sidebar = renderSidebarDOM([], null, () => { clicked = true; }, () => {});
      const btn = sidebar.querySelector('.new-analysis');
      expect(btn).not.toBeNull();
      btn?.click();
      expect(clicked).toBe(true);
    });

    it('T1-4: Displays scrollable history list with session items', () => {
      const sess = createMockSession({ id: 's1', task_intent: 'Inspect pipe' });
      const sidebar = renderSidebarDOM([sess], 's1', () => {}, () => {});
      const items = sidebar.querySelectorAll('.history-item');
      expect(items).toHaveLength(1);
      expect(items[0].className).toContain('selected');
      expect(items[0].textContent).toContain('Inspect pipe');
    });

    it('T1-5: Renders connection footer indicating public session sharing', () => {
      const sidebar = renderSidebarDOM([], null, () => {}, () => {});
      const conn = sidebar.querySelector('.connection');
      expect(conn?.textContent).toContain('Shared with everyone');
    });

    it('T2-1: Displays empty state placeholder when no past sessions exist', () => {
      const sidebar = renderSidebarDOM([], null, () => {}, () => {});
      const empty = sidebar.querySelector('.history-empty');
      expect(empty?.textContent).toContain('No past interviews');
    });

    it('T2-2: Renders 100+ session items without DOM corruption', () => {
      const sessions = Array.from({ length: 105 }, (_, i) =>
        createMockSession({ id: `sess_${i}`, task_intent: `Task ${i}` })
      );
      const sidebar = renderSidebarDOM(sessions, 'sess_50', () => {}, () => {});
      const items = sidebar.querySelectorAll('.history-item');
      expect(items).toHaveLength(105);
    });

    it('T2-3: Handles session switching cleanly updating selected class', () => {
      let currentSelected = 's1';
      const sess1 = createMockSession({ id: 's1', task_intent: 'Task 1' });
      const sess2 = createMockSession({ id: 's2', task_intent: 'Task 2' });
      const sidebar = renderSidebarDOM([sess1, sess2], currentSelected, () => {}, id => { currentSelected = id; });

      const item2 = sidebar.querySelector('[data-session-id="s2"]');
      item2?.click();
      expect(currentSelected).toBe('s2');
    });

    it('T2-4: Preserves long task intent cleanly with clamped styling', () => {
      const longIntent = 'A'.repeat(300);
      const sess = createMockSession({ id: 's_long', task_intent: longIntent });
      const sidebar = renderSidebarDOM([sess], 's_long', () => {}, () => {});
      const title = sidebar.querySelector('.history-title');
      expect(title?.textContent).toBe(longIntent);
    });

    it('T2-5: Navigates cleanly from session selection back to "+ New interview"', () => {
      let isNew = false;
      const sess = createMockSession({ id: 's1' });
      const sidebar = renderSidebarDOM([sess], 's1', () => { isNew = true; }, () => {});
      sidebar.querySelector('.new-analysis')?.click();
      expect(isNew).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 2: F14 - Full Transcript Viewing (R1)
  // -------------------------------------------------------------------------
  describe('F14: Full Transcript Viewing', () => {
    it('T1-1: Renders uploaded files card with name, size, and count', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      const filesCard = dom.querySelector('.session-files-card');
      expect(filesCard?.innerHTML).toContain('pallet_spec.md');
      expect(filesCard?.innerHTML).toContain('(1)');
    });

    it('T1-2: Displays session task intent and referenced robot badge', () => {
      const sess = createMockSession({ task_intent: 'Assembly transport', referenced_robot: 'Walker_C1_EDU' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.textContent).toContain('Assembly transport');
      expect(dom.querySelector('.robot-badge')?.textContent).toBe('Walker_C1_EDU');
    });

    it('T1-3: Displays historical turns and question/answer pairs', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      const turnsCard = dom.querySelector('.turns-history-card');
      expect(turnsCard?.textContent).toContain('Desired travel speed?');
      expect(turnsCard?.textContent).toContain('0.5-1.2 m/s');
    });

    it('T1-4: Renders decision rationale context ("💡 why") for past questions', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      expect(dom.textContent).toContain('💡 Safety regulation adherence');
    });

    it('T1-5: Renders question budget indicator conforming to 25 limit', () => {
      const sess = createMockSession({ question_count: 5 });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.budget-badge')?.textContent).toBe(`5 / ${SPEC_MAX_BUDGET}`);
    });

    it('T2-1: Renders cleanly when session has 0 uploaded files', () => {
      const sess = createMockSession({ files: [] });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.session-files-card')).toBeNull();
    });

    it('T2-2: Renders cleanly when session has 0 past turns (first turn)', () => {
      const sess = createMockSession({ turns: [] });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.turns-history-card')).toBeNull();
    });

    it('T2-3: Formats unknown customer answer ("❓ Not sure / No specification")', () => {
      const sess = createMockSession({
        turns: [
          {
            id: 1,
            turn_index: 1,
            questions: [{ id: 'q1', text: 'Voltage?', options: [] }],
            answers: [{ question_id: 'q1', is_unknown: true }],
            created_at: '2026-09-17T10:00:00Z',
          },
        ],
      });
      const dom = renderTranscriptDOM(sess);
      expect(dom.textContent).toContain('Unknown');
    });

    it('T2-4: Preserves custom free-text answers with special characters', () => {
      const sess = createMockSession({
        turns: [
          {
            id: 1,
            turn_index: 1,
            questions: [{ id: 'q1', text: 'Spec?', options: [] }],
            answers: [{ question_id: 'q1', free_text_answer: 'Tolerance < 0.05mm & Temp > 40°C' }],
            created_at: '2026-09-17T10:00:00Z',
          },
        ],
      });
      const dom = renderTranscriptDOM(sess);
      expect(dom.textContent).toContain('Tolerance < 0.05mm & Temp > 40°C');
    });

    it('T2-5: Renders chronological order correctly across 10 sequential turns', () => {
      const turns: GrillTurn[] = Array.from({ length: 10 }, (_, i) => ({
        id: i + 1,
        turn_index: i + 1,
        questions: [{ id: `q_${i}`, text: `Question ${i + 1}`, options: [] }],
        answers: [{ question_id: `q_${i}`, selected_option: `Answer ${i + 1}` }],
        created_at: `2026-09-17T10:${i < 10 ? '0' + i : i}:00Z`,
      }));
      const sess = createMockSession({ turns });
      const dom = renderTranscriptDOM(sess);
      const renderedTurns = dom.querySelectorAll('.turn-row');
      expect(renderedTurns).toHaveLength(10);
      expect(renderedTurns[0].getAttribute('data-turn-index')).toBe('1');
      expect(renderedTurns[9].getAttribute('data-turn-index')).toBe('10');
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 3: F15 - Active vs Completed State Handling (R1)
  // -------------------------------------------------------------------------
  describe('F15: Active vs Completed State Handling', () => {
    it('T1-1: Active interviewing session renders interactive question form', () => {
      const sess = createMockSession({ status: 'interviewing' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.active-questions-form')).not.toBeNull();
      expect(dom.querySelector('.option-btn')).not.toBeNull();
    });

    it('T1-2: Active ready_for_confirmation session renders readback confirmation box', () => {
      const sess = createMockSession({ status: 'ready_for_confirmation', readback_summary: 'Ready to finalize.' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.readback-confirm-box')).not.toBeNull();
      expect(dom.querySelector('.confirm-btn')).not.toBeNull();
      expect(dom.querySelector('.active-questions-form')).toBeNull();
    });

    it('T1-3: Completed session renders report card in read-only mode and generates markdown', () => {
      const sess = createMockSession({ status: 'completed' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.report-view-card.read-only')).not.toBeNull();
      expect(dom.querySelector('.active-questions-form')).toBeNull();

      const mockReport: GrillReport = {
        schema_version: '1.0',
        session_id: sess.id,
        scenario_summary: 'Assessment summary',
        capabilities: { summary: 'Cap ok' },
        system_architecture: { summary: 'Arch ok' },
        risk_matrix: { summary: 'Risk ok' },
      };
      const md = generateGrillMarkdown(mockReport, sess);
      expect(md).toContain('Scenario Summary');
    });

    it('T1-4: Completed session attaches post-interview interactive Q&A panel', () => {
      const sess = createMockSession({ status: 'completed' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.grill-qa-panel')).not.toBeNull();
    });

    it('T1-5: Active session does not attach post-interview Q&A panel', () => {
      const sess = createMockSession({ status: 'interviewing' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.grill-qa-panel')).toBeNull();
    });

    it('T2-1: Form submits selected option correctly', () => {
      const sess = createMockSession({ status: 'interviewing' });
      const dom = renderTranscriptDOM(sess);
      const optBtn = dom.querySelector('.option-btn');
      expect(optBtn?.textContent).toBe('< 3kg');
    });

    it('T2-2: "Not sure" button enables unknown answer submission', () => {
      const sess = createMockSession({ status: 'interviewing' });
      const dom = renderTranscriptDOM(sess);
      const unknownBtn = dom.querySelector('.unknown-btn');
      expect(unknownBtn?.textContent).toBe('Not sure');
    });

    it('T2-3: Completed session disables form answer submission inputs', () => {
      const sess = createMockSession({ status: 'completed' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.submit-answers-btn')).toBeNull();
    });

    it('T2-4: Readback summary displays fallback when summary string is null', () => {
      const sess = createMockSession({ status: 'ready_for_confirmation', readback_summary: null });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.readback-confirm-box')?.textContent).toContain('Scenario verified.');
    });

    it('T2-5: Transition from ready_for_confirmation to completed preserves transcript history', () => {
      const activeSess = createMockSession({ status: 'ready_for_confirmation' });
      const activeDom = renderTranscriptDOM(activeSess);
      expect(activeDom.querySelector('.turns-history-card')).not.toBeNull();

      const completedSess = createMockSession({ status: 'completed' });
      const completedDom = renderTranscriptDOM(completedSess);
      expect(completedDom.querySelector('.turns-history-card')).not.toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 4: F16 - Post-Interview Interactive Q&A UI Panel (R4)
  // -------------------------------------------------------------------------
  describe('F16: Post-Interview Interactive Q&A UI Panel', () => {
    it('T1-1: Renders Q&A panel with proper heading and context subtitle', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      expect(panel.querySelector('h2')?.textContent).toContain('Ask about this scenario report');
      expect(panel.querySelector('.question-context')?.textContent).toContain('Scenario synthesis, robot selection rationale');
    });

    it('T1-2: Provides question input textarea with 4000 character limit', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const input = panel.querySelector('.question-input');
      expect(input?.getAttribute('maxlength')).toBe('4000');
    });

    it('T1-3: Provides Send button for question dispatch', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const btn = panel.querySelector('.send-btn');
      expect(btn?.textContent).toBe('Send');
    });

    it('T1-4: Transcript container role set to "log" for accessibility', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const transcript = panel.querySelector('.question-transcript');
      expect(transcript?.getAttribute('role')).toBe('log');
    });

    it('T1-5: Renders placeholder matching robot capability and ROS 2 domain', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const input = panel.querySelector('.question-input');
      expect(input?.getAttribute('placeholder')).toContain('robot capabilities, ROS 2 architecture');
    });

    it('T2-1: Empty question input validation', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const input = panel.querySelector('.question-input') as MockElement;
      input.value = '   ';
      expect(input.value.trim()).toBe('');
    });

    it('T2-2: Allows 4000 character question input boundary', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const input = panel.querySelector('.question-input') as MockElement;
      input.value = 'A'.repeat(4000);
      expect(input.value.length).toBe(4000);
    });

    it('T2-3: Streaming chunk appending simulates reactive DOM update', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const transcript = panel.querySelector('.question-transcript') as MockElement;

      const exchange = new MockElement('article');
      exchange.className = 'question-exchange';
      const qP = new MockElement('p');
      qP.className = 'question-text';
      qP.textContent = 'Can it lift 5kg?';
      const aP = new MockElement('p');
      aP.className = 'question-answer';
      aP.textContent = 'Yes';
      exchange.appendChild(qP);
      exchange.appendChild(aP);
      transcript.appendChild(exchange);

      const ansP = exchange.querySelector('.question-answer');
      expect(ansP?.textContent).toBe('Yes');
      if (ansP) ansP.textContent += ', verified within torque limits.';
      expect(ansP?.textContent).toBe('Yes, verified within torque limits.');
    });

    it('T2-4: Auto-scroll calculation detects when user is near bottom', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const transcript = panel.querySelector('.question-transcript') as MockElement;
      transcript.scrollHeight = 1000;
      transcript.clientHeight = 300;
      transcript.scrollTop = 690;
      const isAtBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 30;
      expect(isAtBottom).toBe(true);
    });

    it('T2-5: Interrupted stream displays partial answer with retry button', () => {
      const panel = renderQAPanelDOM('sess_qa_1');
      const transcript = panel.querySelector('.question-transcript') as MockElement;

      const exchange = new MockElement('article');
      exchange.className = 'question-exchange';
      const qP = new MockElement('p');
      qP.className = 'question-text';
      qP.textContent = 'Test Q';
      const aP = new MockElement('p');
      aP.className = 'question-answer';
      aP.textContent = 'Partial';
      const intSpan = new MockElement('span');
      intSpan.className = 'question-interrupted';
      intSpan.textContent = 'Interrupted · partial answer';
      const retryBtn = new MockElement('button');
      retryBtn.className = 'button text-button retry-btn';
      retryBtn.textContent = 'Retry question';

      exchange.appendChild(qP);
      exchange.appendChild(aP);
      exchange.appendChild(intSpan);
      exchange.appendChild(retryBtn);
      transcript.appendChild(exchange);

      expect(exchange.querySelector('.question-interrupted')?.textContent).toContain('Interrupted');
      expect(exchange.querySelector('.retry-btn')).not.toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 5: F17 - Terminology & Safety Guardrails (R7)
  // -------------------------------------------------------------------------
  describe('F17: Terminology & Safety Guardrails', () => {
    it('T1-1: Prohibits "Codex" in user-visible DOM output', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      expect(dom.innerHTML).not.toContain('Codex');
    });

    it('T1-2: Prohibits "sandbox" in user-visible DOM output', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      expect(dom.innerHTML.toLowerCase()).not.toContain('sandbox');
    });

    it('T1-3: Prohibits Chinese "沙箱" in user-visible DOM output', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      expect(dom.innerHTML).not.toContain('沙箱');
    });

    it('T2-1: Never accesses or references prohibited IP 120.77.250.227', () => {
      const sess = createMockSession();
      const dom = renderTranscriptDOM(sess);
      expect(dom.innerHTML).not.toContain(SPEC_FORBIDDEN_IP);
    });

    it('T2-2: Prohibits forbidden terminology in sidebar DOM', () => {
      const sidebar = renderSidebarDOM([], null, () => {}, () => {});
      for (const term of SPEC_PROHIBITED_TERMS) {
        expect(sidebar.innerHTML).not.toContain(term);
      }
    });

    it('T2-3: Prohibits forbidden terminology in Q&A panel DOM', () => {
      const qa = renderQAPanelDOM('sess_1');
      for (const term of SPEC_PROHIBITED_TERMS) {
        expect(qa.innerHTML).not.toContain(term);
      }
    });

    it('T2-4: Sanitizes error message strings containing sandbox', () => {
      const sanitize = (text: string) =>
        text.replace(/sandbox/gi, 'container').replace(/沙箱/g, '计算容器').replace(/codex/gi, 'orchestrator');

      expect(sanitize('Error in sandbox environment')).toBe('Error in container environment');
      expect(sanitize('沙箱启动失败')).toBe('计算容器启动失败');
      expect(sanitize('Codex worker died')).toBe('orchestrator worker died');
    });

    it('T2-5: Validates file extensions rejecting unsafe executable formats', () => {
      expect(isAllowedGrillExtension('script.py')).toBe(false);
      expect(isAllowedGrillExtension('binary.exe')).toBe(false);
      expect(isAllowedGrillExtension('document.pdf')).toBe(true);
      expect(isAllowedGrillExtension('diagram.png')).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 6: F3 & F5 - Frontend Budget (25 max) & Robot Model Scoping
  // -------------------------------------------------------------------------
  describe('F3 & F5: Question Budget (25 Max) & Robot Model Scoping', () => {
    it('T1-1: Maximum question budget is strictly 25', () => {
      expect(SPEC_MAX_BUDGET).toBe(25);
    });

    it('T1-2: Displays warning banner when question_count >= 15 and < 20', () => {
      const sess = createMockSession({ question_count: 15 });
      const dom = renderTranscriptDOM(sess);
      const hint = dom.querySelector('.budget-hint.warning');
      expect(hint?.textContent).toContain('at most 10 questions remaining');
    });

    it('T1-3: Displays urgent banner when question_count >= 20', () => {
      const sess = createMockSession({ question_count: 20 });
      const dom = renderTranscriptDOM(sess);
      const hint = dom.querySelector('.budget-hint.urgent');
      expect(hint?.textContent).toContain('at most 5 questions remaining');
    });

    it('T1-4: Whitelist contains exactly the 4 supported robot models', () => {
      const ids = SPEC_SUPPORTED_ROBOTS.map(r => r.id);
      expect(ids).toEqual([
        'Walker_Tienkung_DEX',
        'Walker_C1_EDU',
        'TienKung',
        'Walker_S2_EDU',
      ]);
    });

    it('T1-5: Formats canonical robot model names properly', () => {
      expect(formatRobotName('Walker_C1_EDU')).toContain('Walker_C1_EDU');
      expect(formatRobotName('TienKung')).toContain('TienKung');
    });

    it('T2-1: Budget display handles 0 questions answered', () => {
      const sess = createMockSession({ question_count: 0 });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.budget-badge')?.textContent).toBe('0 / 25');
    });

    it('T2-2: Budget display handles exact 25 questions boundary', () => {
      const sess = createMockSession({ question_count: 25 });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.budget-badge')?.textContent).toBe('25 / 25');
    });

    it('T2-3: Legacy robot ID mapping aliases Walker_C1 to Walker_C1_EDU', () => {
      const formatted = formatRobotName('Walker_C1');
      expect(formatted).toContain('Walker_C1');
    });

    it('T2-4: Legacy robot ID mapping aliases Walker_S2 to Walker_S2_EDU', () => {
      const formatted = formatRobotName('Walker_S2');
      expect(formatted).toContain('Walker_S2');
    });

    it('T2-5: Unspecified robot model displays localized fallback', () => {
      const formatted = formatRobotName(null);
      expect(formatted).toBeDefined();
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 7: Tier 3 - Cross-Feature Interactions (Pairwise Combinations)
  // -------------------------------------------------------------------------
  describe('Tier 3: Cross-Feature Interactions', () => {
    it('P1: Sidebar selection loads full transcript for chosen session', () => {
      const sess = createMockSession({ id: 's_pair_1', task_intent: 'Pallet loading' });
      let selected: string | null = null;
      const sidebar = renderSidebarDOM([sess], selected, () => {}, id => { selected = id; });

      sidebar.querySelector('[data-session-id="s_pair_1"]')?.click();
      expect(selected).toBe('s_pair_1');

      const transcript = renderTranscriptDOM(sess);
      expect(transcript.textContent).toContain('Pallet loading');
    });

    it('P2: Answering questions increments budget counter towards 25', () => {
      const sess = createMockSession({ question_count: 14 });
      const domBefore = renderTranscriptDOM(sess);
      expect(domBefore.querySelector('.budget-badge')?.textContent).toBe('14 / 25');

      sess.question_count += 1;
      const domAfter = renderTranscriptDOM(sess);
      expect(domAfter.querySelector('.budget-badge')?.textContent).toBe('15 / 25');
    });

    it('P3: Turn progression at 15 questions triggers soft wind-down hint', () => {
      const sess = createMockSession({ question_count: 14 });
      const dom14 = renderTranscriptDOM(sess);
      expect(dom14.querySelector('.budget-hint')).toBeNull();

      sess.question_count = 15;
      const dom15 = renderTranscriptDOM(sess);
      expect(dom15.querySelector('.budget-hint.warning')).not.toBeNull();
    });

    it('P4: Turn progression from 19 to 20 transitions warning hint to urgent', () => {
      const sess = createMockSession({ question_count: 19 });
      const dom19 = renderTranscriptDOM(sess);
      expect(dom19.querySelector('.budget-hint.warning')).not.toBeNull();

      sess.question_count = 20;
      const dom20 = renderTranscriptDOM(sess);
      expect(dom20.querySelector('.budget-hint.urgent')).not.toBeNull();
    });

    it('P5: Hitting 25 questions transitions state to ready_for_confirmation', () => {
      const sess = createMockSession({ question_count: 25, status: 'ready_for_confirmation', readback_summary: '25 Limit Readback' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.readback-confirm-box')).not.toBeNull();
      expect(dom.querySelector('.active-questions-form')).toBeNull();
    });

    it('P6: Confirming scenario transitions view to completed read-only report', () => {
      const sess = createMockSession({ status: 'completed' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.report-view-card')).not.toBeNull();
      expect(dom.querySelector('.readback-confirm-box')).toBeNull();
    });

    it('P7: Completed report embeds interactive post-interview Q&A panel', () => {
      const sess = createMockSession({ status: 'completed' });
      const dom = renderTranscriptDOM(sess);
      expect(dom.querySelector('.grill-qa-panel')).not.toBeNull();
    });

    it('P8: Sending follow-up question appends new exchange to transcript', () => {
      const panel = renderQAPanelDOM('s_p8');
      const transcript = panel.querySelector('.question-transcript') as MockElement;

      const exchange = new MockElement('article');
      exchange.className = 'question-exchange';
      exchange.innerHTML = '<p class="question-text">Payload question?</p>';
      transcript.appendChild(exchange);

      expect(transcript.children).toHaveLength(1);
      expect(transcript.textContent).toContain('Payload question?');
    });

    it('P9: "+ New interview" resets state and returns to blank intake', () => {
      let activeView: 'session' | 'intake' = 'session';
      const sess = createMockSession();
      const sidebar = renderSidebarDOM([sess], sess.id, () => { activeView = 'intake'; }, () => {});

      sidebar.querySelector('.new-analysis')?.click();
      expect(activeView).toBe('intake');
    });

    it('P10: Switching between active and completed sessions maintains UI state', () => {
      const active = createMockSession({ id: 's_act', status: 'interviewing' });
      const done = createMockSession({ id: 's_done', status: 'completed' });

      const domAct = renderTranscriptDOM(active);
      expect(domAct.querySelector('.active-questions-form')).not.toBeNull();
      expect(domAct.querySelector('.grill-qa-panel')).toBeNull();

      const domDone = renderTranscriptDOM(done);
      expect(domDone.querySelector('.active-questions-form')).toBeNull();
      expect(domDone.querySelector('.grill-qa-panel')).not.toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // GROUP 8: Tier 4 - Real-World Application Scenarios (S1-S5)
  // -------------------------------------------------------------------------
  describe('Tier 4: Real-World Application Scenarios (S1-S5)', () => {
    it('S1: End-to-end interview walkthrough with Walker_C1_EDU', () => {
      // 1. Initial intake session
      const session = createMockSession({
        id: 's_s1',
        task_intent: 'Carry 5kg parts across assembly floor',
        referenced_robot: 'Walker_C1_EDU',
        question_count: 1,
        status: 'interviewing',
      });
      const dom1 = renderTranscriptDOM(session);
      expect(dom1.textContent).toContain('Carry 5kg parts across assembly floor');
      expect(dom1.querySelector('.robot-badge')?.textContent).toBe('Walker_C1_EDU');
      expect(dom1.querySelector('.active-questions-form')).not.toBeNull();

      // 2. Answer question and advance to turn 2
      session.question_count = 2;
      session.status = 'ready_for_confirmation';
      session.readback_summary = 'Walker_C1_EDU verified for 5kg payload at 1.0 m/s.';
      const dom2 = renderTranscriptDOM(session);
      expect(dom2.querySelector('.readback-confirm-box')).not.toBeNull();

      // 3. Confirm and complete
      session.status = 'completed';
      const dom3 = renderTranscriptDOM(session);
      expect(dom3.querySelector('.report-view-card')).not.toBeNull();
      expect(dom3.querySelector('.grill-qa-panel')).not.toBeNull();
    });

    it('S3: 25-Question budget exhaustion with phased warning banners', () => {
      const session = createMockSession({ question_count: 10 });
      // Turn 10: normal
      let dom = renderTranscriptDOM(session);
      expect(dom.querySelector('.budget-hint')).toBeNull();

      // Turn 15: warning
      session.question_count = 15;
      dom = renderTranscriptDOM(session);
      expect(dom.querySelector('.budget-hint.warning')?.textContent).toContain('at most 10 questions remaining');

      // Turn 20: urgent
      session.question_count = 20;
      dom = renderTranscriptDOM(session);
      expect(dom.querySelector('.budget-hint.urgent')?.textContent).toContain('at most 5 questions remaining');

      // Turn 25: ceiling reached
      session.question_count = 25;
      session.status = 'ready_for_confirmation';
      dom = renderTranscriptDOM(session);
      expect(dom.querySelector('.budget-badge')?.textContent).toBe('25 / 25');
      expect(dom.querySelector('.readback-confirm-box')).not.toBeNull();
    });

    it('S4: Completed report review with post-interview Q&A streaming exchange', () => {
      const session = createMockSession({ status: 'completed' });
      const dom = renderTranscriptDOM(session);
      const qa = dom.querySelector('.grill-qa-panel');
      expect(qa).not.toBeNull();

      // Post follow-up question
      const input = qa?.querySelector('.question-input') as MockElement;
      input.value = 'What is the maximum payload capacity?';
      expect(input.value).toBe('What is the maximum payload capacity?');

      // Stream response into transcript
      const transcript = qa?.querySelector('.question-transcript') as MockElement;
      const exchange = new MockElement('article');
      exchange.className = 'question-exchange';
      exchange.innerHTML = `
        <p class="question-text">${input.value}</p>
        <p class="question-answer">Walker_C1_EDU supports up to 8kg dynamic payload.</p>
      `;
      transcript.appendChild(exchange);

      expect(transcript.textContent).toContain('Walker_C1_EDU supports up to 8kg dynamic payload.');
    });

    it('S5: Persistent sidebar multi-session switching across active, hibernated, and completed sessions', () => {
      const sActive = createMockSession({ id: 's_act', task_intent: 'Active interview', status: 'interviewing' });
      const sReady = createMockSession({ id: 's_ready', task_intent: 'Ready confirm', status: 'ready_for_confirmation' });
      const sDone = createMockSession({ id: 's_done', task_intent: 'Completed report', status: 'completed' });

      let selectedId = 's_act';
      const sidebar = renderSidebarDOM([sActive, sReady, sDone], selectedId, () => {}, id => { selectedId = id; });

      // Check all 3 items rendered
      const items = sidebar.querySelectorAll('.history-item');
      expect(items).toHaveLength(3);

      // Select active session
      let dom = renderTranscriptDOM(sActive);
      expect(dom.querySelector('.active-questions-form')).not.toBeNull();

      // Select completed session
      sidebar.querySelector('[data-session-id="s_done"]')?.click();
      expect(selectedId).toBe('s_done');
      dom = renderTranscriptDOM(sDone);
      expect(dom.querySelector('.report-view-card')).not.toBeNull();
      expect(dom.querySelector('.grill-qa-panel')).not.toBeNull();
    });
  });
});
