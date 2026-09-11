import type { BackendObservation } from './types.ts';

export const observationPeriodKey = (point: BackendObservation) =>
  JSON.stringify([
    point.period.basis,
    point.period.start,
    point.period.end,
    point.period.instant,
    point.period.label,
  ]);

// One filed fact can produce multiple observation occurrences. Collapse only
// identical source facts, never different values or independent source facts.
export function uniqueObservationPeriods(points: BackendObservation[]) {
  const periods = new Map<string, BackendObservation[]>();
  for (const point of points) {
    const key = observationPeriodKey(point);
    periods.set(key, [...(periods.get(key) || []), point]);
  }
  let duplicateCount = 0;
  let conflictCount = 0;
  const unique: BackendObservation[] = [];
  for (const occurrences of periods.values()) {
    const identities = new Set(
      occurrences.map((point) => {
        if (
          !point.source.sourceFactId ||
          !(point.source.filingId || point.source.filingRef)
        )
          return point.id;
        // Source-regime labels can differ when Form 6 and Page 700 emit the
        // same fact. Every substantive field, including quality, must agree.
        const { id, sourceRegime, ...fact } = point;
        void id;
        void sourceRegime;
        return JSON.stringify(fact);
      }),
    );
    if (identities.size !== 1) {
      conflictCount += 1;
      continue;
    }
    duplicateCount += occurrences.length - 1;
    unique.push(occurrences[0]);
  }
  return {
    points: unique.sort((a, b) => a.sortKey.localeCompare(b.sortKey)),
    duplicateCount,
    conflictCount,
  };
}
