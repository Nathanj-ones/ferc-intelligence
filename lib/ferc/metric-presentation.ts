import type { BackendMetricSeries, BackendObservation } from './types.ts';
import { unitLabel } from './format.ts';

export const requiresQuantityDenominator = (metricId: string) =>
  [
    'ioc_top5_shipper_concentration',
    'ioc_affiliate_share',
    'ioc_identity_coverage',
  ].includes(metricId);

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
  if (requiresQuantityDenominator(metric.id)) {
    const denominator = concentrationDenominator(points);
    if (
      metric.id !== 'ioc_top5_shipper_concentration' &&
      denominator === 'ioc_contracted_storage_quantity'
    ) {
      const unknownAffiliate =
        metric.id === 'ioc_affiliate_share' &&
        points.every((point) =>
          /affiliate flag blank or unrecognised/i.test(point.scope.actual),
        );
      return {
        label:
          metric.id === 'ioc_identity_coverage'
            ? 'Shipper identity coverage of contracted storage quantity'
            : unknownAffiliate
              ? 'Unclassified affiliate share of contracted storage quantity'
              : 'Affiliate share of contracted storage quantity',
        description:
          metric.id === 'ioc_identity_coverage'
            ? 'Share of matched contracted storage quantity with a resolved shipper identity; this is not weighted by daily transport capacity.'
            : unknownAffiliate
              ? 'Share of matched contracted storage quantity with a blank or unrecognised affiliate flag; this is not a confirmed affiliate holding.'
              : 'Share of matched contracted storage quantity held by contracts flagged as affiliate; this is not weighted by daily transport capacity.',
      };
    }
    if (
      metric.id !== 'ioc_top5_shipper_concentration' &&
      denominator === 'mixed'
    ) {
      return {
        label: metric.label,
        description:
          'Coverage varies by filed scope and denominator. Select a scope to distinguish storage quantity from daily transport capacity; they are not comparable weights.',
      };
    }
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
