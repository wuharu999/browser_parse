import { describe, expect, it, vi } from 'vitest';
vi.mock('../src/i18n', () => ({ t: (en: string) => en }));
import { readAnswer, type ChatEvent } from '../src/questions';

function event(type: string, answer = ''): string {
  return JSON.stringify({ type, item: { id: 1, question: '为何？', answer, status: type === 'done' ? 'completed' : 'generating' } }) + '\n';
}

describe('question answer streaming', () => {
  it('renders partial output before completion and decodes split UTF-8 frames', async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const response = new Response(new ReadableStream({ start(value) { controller = value; } }));
    const received: ChatEvent[] = [];
    const reading = readAnswer(response, e => received.push(e));
    const bytes = new TextEncoder().encode(event('started') + event('answer', '电机停机 [E1]'));
    for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.slice(i, i + 1));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(received.map(e => e.type)).toEqual(['started', 'answer']);
    expect(received[1].item.answer).toBe('电机停机 [E1]');
    controller.enqueue(new TextEncoder().encode(event('done', '电机停机 [E1]。'))); controller.close();
    await reading;
    expect(received.at(-1)?.type).toBe('done');
  });

  it('keeps the partial answer when a stream ends early', async () => {
    const received: ChatEvent[] = [];
    await expect(readAnswer(new Response(event('answer', 'Partial')), e => received.push(e))).rejects.toThrow('interrupted');
    expect(received[0].item.answer).toBe('Partial');
  });

  it('accepts an explicit interrupted result and rejects malformed events', async () => {
    await readAnswer(new Response(event('interrupted', 'Partial')), () => {});
    await expect(readAnswer(new Response('{"type":"unexpected"}\n'), () => {})).rejects.toThrow();
  });
});
