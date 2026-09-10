export type EvidenceLink = { id: string; observation: string; source: string; lines: string; excerpt: string; reasoning: string };
export type AnalysisReport = { summary: string; evidenceChain: EvidenceLink[]; workflow: string[]; uncertainties: string[]; demo: boolean; structured: boolean };

const string = (value: unknown): string => typeof value === 'string' ? value : '';
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];

/** Legacy reports remain readable; malformed structure never becomes invented evidence. */
export function parseReport(raw: unknown): AnalysisReport {
  const fallback: AnalysisReport = { summary: string(raw), evidenceChain: [], workflow: [], uncertainties: [], demo: false, structured: false };
  if (typeof raw !== 'string') return fallback;
  try {
    const value = JSON.parse(raw.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, ''));
    if (!value || value.schemaVersion !== 'robot-analysis/v1' || typeof value.summary !== 'string') return fallback;
    const evidenceChain: EvidenceLink[] = Array.isArray(value.evidenceChain) ? value.evidenceChain.filter((item: unknown) => item && typeof item === 'object').map((item: Record<string, unknown>) => ({ id: string(item.id), observation: string(item.observation), source: string(item.source), lines: string(item.lines), excerpt: string(item.excerpt), reasoning: string(item.reasoning) })) : [];
    return { summary: value.summary, evidenceChain, workflow: strings(value.workflow), uncertainties: strings(value.uncertainties), demo: value.demo === true, structured: true };
  } catch { return fallback; }
}
