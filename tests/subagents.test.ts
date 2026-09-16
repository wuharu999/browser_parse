import { describe, expect, it } from 'vitest';
import { observedAgents, subagentBadge, subagentStates, type SubagentSummary } from '../src/subagents';
const child: SubagentSummary = { thread_id: 'child-a', parent_thread_id: 'parent', status: 'running', tool: 'spawn_agent', seq: 10 };
describe('observed subagent lifecycle', () => {
  it('retains children outside the latest event page and ignores older states', () => {
    expect(subagentStates([child], [{ seq: 2, subagent: { ...child, status: 'pending_init' } }, { seq: 600 }])).toEqual([child]);
  });
  it('updates by event sequence and keeps each thread distinct', () => {
    const states = subagentStates([child], [
      { seq: 11, subagent: { ...child, status: 'completed', tool: 'wait' } },
      { seq: 12, subagent: { ...child, thread_id: 'child-b' } },
    ]);
    expect(states.map(c => [c.thread_id, c.status])).toEqual([['child-a', 'completed'], ['child-b', 'running']]);
  });
  it('does not claim child completion from job completion or spawn tool completion', () => {
    expect(subagentBadge(child, false).en).toBe('Running');
    expect(subagentBadge(child, true)).toMatchObject({ en: 'Last observed: running', cls: 'idle' });
    expect(subagentBadge({ ...child, status: 'completed', tool: 'wait' }, true).en).toBe('Completed');
  });
  it('does not invent invocations when historical events lack identity', () => {
    expect(subagentStates(undefined, [{ seq: 1, subagent: null }])).toEqual([]);
  });
});

describe('only instantiated agents are shown', () => {
  it('does not show waiting role cards or a parent before it starts', () => {
    expect(observedAgents(undefined, [])).toEqual({parent: false, children: []});
    expect(observedAgents(undefined, [{seq: 1, agent: 'worker'}]).parent).toBe(false);
  });
  it('shows the parent after startup and only observed children', () => {
    expect(observedAgents({codex_started: true}, []).parent).toBe(true);
    expect(observedAgents(undefined, [{seq: 1, agent: 'codex'}]).parent).toBe(true);
    expect(observedAgents({subagents: [child]}, []).children).toEqual([child]);
  });
  it('keeps a known role and state when wait-begin supplies neither', () => {
    const previous = {...child, role: 'log_investigator'};
    const current = subagentStates([previous], [{seq: 12, subagent: {...child, status: 'unknown', tool: 'wait'}}]);
    expect(current[0]).toMatchObject({role: 'log_investigator', status: 'running', seq: 12});
  });
});
