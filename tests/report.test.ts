import { describe, expect, it } from 'vitest';
import { parseReport } from '../src/report';

describe('analysis report presentation', () => {
  it('keeps legacy and invalid reports readable without manufacturing a chain', () => {
    expect(parseReport('Legacy findings')).toMatchObject({ summary: 'Legacy findings', evidenceChain: [], structured: false });
    expect(parseReport('{broken').structured).toBe(false);
  });
  it('reads bounded structured output and keeps source/user text verbatim', () => {
    const raw = JSON.stringify({ schemaVersion: 'robot-analysis/v1', summary: '通信异常', evidenceChain: [{ id: 'E1', observation: '<script>test</script>', source: 'robot.log', lines: '2–3' }], workflow: ['Check E1', 2], uncertainties: ['Root cause unconfirmed'], demo: true });
    expect(parseReport('```json\n'+raw+'\n```')).toMatchObject({ summary: '通信异常', workflow: ['Check E1'], demo: true, evidenceChain: [{ observation: '<script>test</script>', source: 'robot.log', lines: '2–3' }] });
  });
});
