export type SubagentState = {
  thread_id: string;
  parent_thread_id: string | null;
  status: 'pending_init' | 'running' | 'interrupted' | 'completed' | 'errored' | 'shutdown' | 'not_found' | 'unknown';
  tool: 'spawn_agent' | 'send_input' | 'wait' | 'close_agent';
  role?: string;
};
export type SubagentSummary = SubagentState & { seq: number };

/** Merge full-history summaries with newly polled events without regressing on older pages. */
export function subagentStates(summary: SubagentSummary[] = [], events: { seq: number; subagent?: SubagentState | null }[] = []): SubagentSummary[] {
  const children = new Map<string, SubagentSummary>();
  for (const state of [...summary, ...events.flatMap(event => event.subagent ? [{ ...event.subagent, seq: event.seq }] : [])]) {
    const previous = children.get(state.thread_id);
    if (!previous || state.seq > previous.seq) children.set(state.thread_id, state);
  }
  return [...children.values()].sort((a, b) => a.thread_id.localeCompare(b.thread_id));
}

/** A job ending cannot establish that every child completed successfully. */
export function subagentBadge(state: SubagentState, jobEnded: boolean): { en: string; zh: string; cls: string } {
  const labels = {
    pending_init: ['Initializing', '初始化中', 'active'],
    running: ['Running', '执行中', 'active'],
    interrupted: ['Interrupted', '已中断', 'stopped'],
    completed: ['Completed', '已完成', 'completed'],
    errored: ['Failed', '失败', 'stopped'],
    shutdown: ['Shut down', '已关闭', 'stopped'],
    not_found: ['Not found', '未找到', 'idle'],
    unknown: ['Unknown', '未知', 'idle'],
  } as const;
  const [en, zh, cls] = labels[state.status] ?? labels.unknown;
  const stale = jobEnded && ['pending_init', 'running'].includes(state.status);
  return { en: stale ? `Last observed: ${en.toLowerCase()}` : en, zh: stale ? `最后记录：${zh}` : zh, cls: stale ? 'idle' : cls };
}
