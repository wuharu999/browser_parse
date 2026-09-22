import { t } from '../i18n';
import type { CustomerAnswer, GrillSession } from './types';

export function formatAnswer(answer: CustomerAnswer): string {
  if (answer.unknown) return t('Unknown', '暂不确定');
  return [answer.selected_option, answer.free_text].filter(Boolean).join(' — ');
}

/** Shared attachment and transcript rendering for interviews and final reports. */
export function sessionContextCards(session: GrillSession, token = session.token || ''): HTMLElement[] {
  const cards: HTMLElement[] = [];
  if (session.files?.length) {
    const card = document.createElement('div');
    card.className = 'grill-card session-files-card';
    const title = document.createElement('h3');
    title.textContent = `${t('Uploaded Documents & Diagrams', '上传参考文档与图纸')} (${session.files.length})`;
    card.appendChild(title);
    for (const file of session.files) {
      const row = document.createElement('div');
      row.className = 'file-item';
      const link = document.createElement('a');
      link.className = 'file-link';
      link.href = `/api/grill/sessions/${encodeURIComponent(session.id)}/files/${encodeURIComponent(file.id)}?token=${encodeURIComponent(token)}`;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = `📎 ${file.name} (${file.size} B)`;
      row.appendChild(link);
      card.appendChild(row);
    }
    cards.push(card);
  }
  if (session.turns?.length) {
    const card = document.createElement('div');
    card.className = 'grill-card turns-history-card';
    const title = document.createElement('h3');
    title.textContent = t('Past Turn Transcript', '往轮问答推演记录');
    card.appendChild(title);
    for (const turn of session.turns) {
      const row = document.createElement('div');
      row.className = 'turn-row';
      row.dataset.turnIndex = String(turn.turn_index);
      const heading = document.createElement('div');
      heading.className = 'turn-row-header';
      heading.textContent = `${t('Turn', '第')} ${turn.turn_index} ${t('', '轮问答')}`;
      row.appendChild(heading);
      for (const question of turn.questions) {
        const text = document.createElement('div');
        text.className = 'past-question-box';
        text.textContent = `Q: ${question.text}${question.why ? ` (💡 ${question.why})` : ''}`;
        row.appendChild(text);
        const answer = turn.answers.find(item => item.question_id === question.id);
        if (answer) {
          const reply = document.createElement('div');
          reply.className = 'past-answer-box';
          reply.textContent = `A: ${formatAnswer(answer)}`;
          row.appendChild(reply);
        }
      }
      card.appendChild(row);
    }
    cards.push(card);
  }
  return cards;
}
