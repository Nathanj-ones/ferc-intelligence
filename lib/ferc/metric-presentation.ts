import type { BackendMetricSeries, BackendObservation } from './types.ts';
import { unitLabel } from './format.ts';

export function concentrationComparisonKey(points: BackendObservation[]) {
  const keys = points.map((point) => {
    const edges = (point.lineage?.edgeSample || []).filter(
      (edge) => edge.input_role === 'denominator',
    );
    const denominator = edges[0];
    if (
      edges.length !== 1 ||
      !['ioc_contracted_storage_quantity', 'ioc_firm_transport_mdq'].includes(
        String(denominator.input_concept),
      ) ||
      typeof denominator.input_unit !== 'string' ||
      !denominator.input_unit.trim()
    )
      return null;
    return JSON.stringify([
      denominator.input_concept,
      unitLabel(denominator.input_unit),
    ]);
  });
  return keys.length > 0 && keys.every((key) => key && key === keys[0])
    ? keys[0]
    : null;
}

export function concentrationDenominator(points: BackendObservation[]) {
  const concepts = new Set(
    points.flatMap((point) =>
      (point.lineage?.edgeSample || [])
        .map((edge) => edge.input_concept)
        .filter(
          (concept) =>
            concept === 'ioc_contracted_storage_quantity' ||
            concept === 'ioc_firm_transport_mdq',
        ),
    ),
  );
  return concepts.size === 1
    ? ([...concepts][0] as string)
    : concepts.size > 1
      ? 'mixed'
      : 'unknown';
}

export function metricPresentation(
  metric: BackendMetricSeries,
  points: BackendObservation[] = metric.points,
) {
  if (metric.id === 'ioc_top5_shipper_concentration') {
    const denominator = concentrationDenominator(points);
    if (
      denominator === 'ioc_contracted_storage_quantity' ||
      denominator === 'mixed'
    ) {
      return denominator === 'ioc_contracted_storage_quantity'
        ? {
            label: 'Top-five shipper share of contracted storage quantity',
            description:
              'Share of matched contracted storage quantity held by the five largest shippers in the filed population. This is storage inventory quantity, not daily transport capacity.',
          }
        : {
            label: 'Top-five shipper concentration',
            description:
              'Share of contracted quantity held by the five largest shippers. Select a filed scope to distinguish storage quantity from daily transport capacity.',
          };
    }
  }
  return { label: metric.label, description: metric.description };
}

export function observationFiledValues(point: BackendObservation) {
  return {
    storedValue: point.value.as_filed ?? undefined,
    filedValue:
      point.source.fact?.valueAsFiled ?? point.value.as_filed ?? undefined,
  };
}
