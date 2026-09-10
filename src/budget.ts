export type Budget = {
  day: string; timezone: string; resets_at: string;
  daily_limit_usd: number; estimated_per_job_usd: number; spent_usd: number;
  active_reservations_usd: number; admission_used_usd: number;
  max_running: number; running: number; max_pending: number; pending: number; queued: number;
  resource_wait?: boolean;
  resource_pool?: { cpu_milli: number; memory_mb: number; disk_mb: number };
  resources_used?: { cpu_milli: number; memory_mb: number; disk_mb: number };
};

export function availability(budget: Budget | null) {
  if (!budget || ![budget.daily_limit_usd, budget.estimated_per_job_usd, budget.spent_usd, budget.active_reservations_usd, budget.admission_used_usd, budget.max_running, budget.running, budget.max_pending, budget.pending, budget.queued].every(value => Number.isFinite(value) && value >= 0) || budget.estimated_per_job_usd <= 0) {
    return { state: 'unknown', canSubmit: false, remaining: 0, reservations: 0, settledPercent: 0, reservedPercent: 0 };
  }
  const remaining = Math.max(0, budget.daily_limit_usd - budget.admission_used_usd);
  const reservations = Math.floor(remaining / budget.estimated_per_job_usd);
  const settledPercent = budget.daily_limit_usd > 0 ? Math.min(100, 100 * budget.spent_usd / budget.daily_limit_usd) : 0;
  const reservedPercent = budget.daily_limit_usd > 0 ? Math.min(100 - settledPercent, 100 * budget.active_reservations_usd / budget.daily_limit_usd) : 0;
  const state = budget.pending >= budget.max_pending ? 'queue_full' : reservations < 1 ? 'budget_wait' : budget.resource_wait || budget.running >= budget.max_running ? 'capacity_wait' : budget.queued > 0 ? 'queue_wait' : 'available';
  return { state, canSubmit: state !== 'queue_full', remaining, reservations, settledPercent, reservedPercent };
}
