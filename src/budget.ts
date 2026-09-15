export type Budget = {
  day: string; timezone: string; resets_at: string;
  daily_limit_shots?: number; completed_today?: number; used_today?: number; remaining_shots?: number;
  daily_limit_usd?: number; estimated_per_job_usd?: number; spent_usd?: number;
  active_reservations_usd?: number; admission_used_usd?: number;
  max_running: number; running: number; max_pending: number; pending: number; queued: number;
  resource_envelope?: { cpu_milli: number; memory_mb: number; disk_mb: number };
};

export function availability(budget: Budget | null) {
  if (!budget) {
    return { state: 'unknown', canSubmit: false, remaining: 0, reservations: 0, settledPercent: 0, reservedPercent: 0 };
  }
  const limit = budget.daily_limit_shots ?? budget.daily_limit_usd;
  const estimate = budget.estimated_per_job_usd ?? 1;
  const used = budget.used_today ?? budget.admission_used_usd;
  const spent = budget.completed_today ?? budget.spent_usd ?? 0;
  const reserved = budget.daily_limit_shots !== undefined ? budget.running : (budget.active_reservations_usd ?? budget.running ?? 0);

  if (limit === undefined || used === undefined || !Number.isFinite(limit) || limit <= 0 || !Number.isFinite(used) || used < 0 || !Number.isFinite(estimate) || estimate <= 0) {
    return { state: 'unknown', canSubmit: false, remaining: 0, reservations: 0, settledPercent: 0, reservedPercent: 0 };
  }

  const remaining = Math.max(0, limit - used);
  const reservations = budget.estimated_per_job_usd !== undefined ? Math.floor(remaining / estimate) : remaining;
  const settledPercent = limit > 0 ? Math.min(100, 100 * spent / limit) : 0;
  const reservedPercent = limit > 0 ? Math.min(100 - settledPercent, 100 * reserved / limit) : 0;
  const state = budget.pending >= budget.max_pending ? 'queue_full' : reservations < 1 ? 'budget_wait' : budget.running >= budget.max_running ? 'capacity_wait' : budget.queued > 0 ? 'queue_wait' : 'available';
  return { state, canSubmit: state !== 'queue_full', remaining, reservations, settledPercent, reservedPercent };
}

