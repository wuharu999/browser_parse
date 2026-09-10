/** Bounded, deterministic sampling balanced by archive, subsystem, then file.
 * Exact strings (including numbers) define duplicates; only three examples survive.
 */
export interface Sample {
  sourceIndex: number; subsystem: string; sourceId: string;
  signature: string; severity: string; line: number; priority: number;
}

export class BalancedSample<T extends Sample> {
  readonly items: T[] = [];
  constructor(readonly capacity: number) {}

  offer(item: T): T | undefined {
    if (this.capacity <= 0) return item;
    const duplicates = this.items.filter(value => value.sourceId === item.sourceId && value.signature === item.signature && value.severity === item.severity);
    if (duplicates.length >= 3) {
      // Keep the first two and latest occurrence without an unbounded history.
      const latest = duplicates.reduce((a, b) => a.line > b.line ? a : b);
      this.items[this.items.indexOf(latest)] = item;
      return latest;
    }
    if (this.items.length < this.capacity) { this.items.push(item); return undefined; }
    let candidates = [...this.items];
    let underrepresented = false;
    for (const key of ['sourceIndex', 'subsystem', 'sourceId'] as const) {
      const groups = new Map<string | number, T[]>();
      for (const value of candidates) {
        const group = groups.get(value[key]) ?? [];
        group.push(value); groups.set(value[key], group);
      }
      const own = groups.get(item[key]) ?? [];
      const largest = [...groups.values()].sort((a, b) => b.length - a.length)[0];
      if (largest.length > own.length) { candidates = largest; underrepresented = true; break; }
      candidates = own;
    }
    const counts = new Map<string, number>();
    const repeatKey = (value: T) => JSON.stringify([value.severity, value.signature]);
    for (const value of candidates) counts.set(repeatKey(value), (counts.get(repeatKey(value)) ?? 0) + 1);
    const repetitions = (value: T) => counts.get(repeatKey(value)) ?? 0;
    // Protect high severity and rare signatures inside each fair share.
    candidates.sort((a, b) => a.priority - b.priority || repetitions(b) - repetitions(a) || a.line - b.line);
    const victim = candidates[0];
    if (!victim || (!underrepresented && item.priority < victim.priority)) return item;
    if (!underrepresented && item.priority === victim.priority && duplicates.length && repetitions(victim) <= duplicates.length) return item;
    this.items[this.items.indexOf(victim)] = item;
    return victim;
  }
}
