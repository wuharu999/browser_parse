import { describe, expect, it } from 'vitest';
import { availability, type Budget } from '../src/budget';

const budget: Budget = { day: '2026-09-10', timezone: 'Asia/Shanghai', resets_at: '2026-09-11T00:00:00+08:00', daily_limit_usd: 10, estimated_per_job_usd: 5, spent_usd: 0, active_reservations_usd: 0, admission_used_usd: 0, max_running: 2, running: 0, max_pending: 20, pending: 0, queued: 0 };
describe('shared budget availability', () => {
  it('distinguishes admission allowance from a guaranteed worker start', () => {
    expect(availability(budget)).toEqual({ state: 'available', canSubmit: true, remaining: 10, reservations: 2, settledPercent: 0, reservedPercent: 0 });
  });
  it('allows queuing when the remaining allowance cannot cover a job', () => {
    expect(availability({ ...budget, spent_usd: 7, admission_used_usd: 7 })).toMatchObject({ state: 'budget_wait', canSubmit: true, remaining: 3, reservations: 0 });
  });
  it('counts cross-day running reservations and clamps an overrun bar', () => {
    expect(availability({ ...budget, spent_usd: 8, active_reservations_usd: 5, admission_used_usd: 13 })).toMatchObject({ state: 'budget_wait', remaining: 0, settledPercent: 80, reservedPercent: 20 });
  });
  it('distinguishes worker capacity and queued work ahead', () => {
    expect(availability({ ...budget, running: 2 }).state).toBe('capacity_wait');
    expect(availability({ ...budget, pending: 1, queued: 1 }).state).toBe('queue_wait');
  });
  it('blocks creation only when pending slots are full or status is unavailable', () => {
    expect(availability({ ...budget, pending: 20 })).toMatchObject({ state: 'queue_full', canSubmit: false });
    expect(availability(null)).toMatchObject({ state: 'unknown', canSubmit: false });
    expect(availability({ ...budget, daily_limit_usd: NaN })).toMatchObject({ state: 'unknown', canSubmit: false });
  });
});
