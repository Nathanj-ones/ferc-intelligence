import {
  isMetricPresentationBlocked,
  isStructuredFercValue,
  presentationUnit,
  selectBackendDisplayValue,
  unitLabel,
} from './format.ts';
import type { BackendMetricSeries, BackendObservation } from './types.ts';

export type KeyMetricCategory =
  | 'Performance'
  | 'Activity'
  | 'Commercial'
  | 'Regulation'
  | 'Profile'
  | 'Status';

type KeyMetricStrategy =
  | 'quarter'
  | 'annual'
  | 'snapshot'
  | 'single-series'
  | 'latest-event';

export type KeyMetricDefinition = {
  metricId: string;
  label?: string;
  category: KeyMetricCategory;
  strategy: KeyMetricStrategy;
  secondaryMetricIds?: string[];
  fallbackOnly?: boolean;
};

export type KeyMetricAssetContext = {
  regime: string;
  scopeRelation?: string | null;
};

export type SelectedKeyMetric = {
  definition: KeyMetricDefinition;
  metric: BackendMetricSeries;
  point: BackendObservation;
  priorYearPoint: BackendObservation | null;
  secondary: {
    metric: BackendMetricSeries;
    point: BackendObservation;
  }[];
};

export const keyMetricScopeLabel = (point: BackendObservation) => {
  const segments = point.scope.actual
    .split('|')
    .map((segment) => segment.trim())
    .filter(Boolean);
  const facility = segments[1] || segments[0];
  const component = segments[2];
  const componentIsScope =
    component &&
    !/^(?:facility (?:total|scope)|authori[sz]ation\b|commissioning\b|operational\b|inspection\b|request\b|order\b|filing\b|dockets?\b)/i.test(
      component,
    );
  return (
    [facility, componentIsScope ? component : null].filter(Boolean).join(' · ') ||
    'Filed entity scope'
  );
};

const templates: Record<string, KeyMetricDefinition[]> = {
  'Interstate gas': [
    {
      metricId: 'gas_operating_revenues',
      category: 'Performance',
      strategy: 'quarter',
    },
    {
      metricId: 'net_utility_operating_income',
      category: 'Performance',
      strategy: 'quarter',
      secondaryMetricIds: ['operating_margin_pct'],
    },
    {
      metricId: 'total_throughput',
      category: 'Activity',
      strategy: 'quarter',
    },
    {
      metricId: 'ioc_firm_transport_mdq',
      category: 'Commercial',
      strategy: 'snapshot',
      secondaryMetricIds: ['ioc_mdq_change'],
    },
    {
      metricId: 'ioc_top5_shipper_concentration',
      category: 'Commercial',
      strategy: 'snapshot',
    },
  ],
  'Gas storage': [
    {
      metricId: 'storage_capacity',
      category: 'Profile',
      strategy: 'single-series',
    },
    {
      metricId: 'max_day_withdrawal',
      category: 'Activity',
      strategy: 'single-series',
    },
    {
      metricId: 'ioc_contracted_storage_quantity',
      category: 'Commercial',
      strategy: 'snapshot',
    },
    {
      metricId: 'ioc_top5_shipper_concentration',
      label: 'Top-five shipper share of contracted storage quantity',
      category: 'Commercial',
      strategy: 'snapshot',
    },
    {
      metricId: 'gas_operating_revenues',
      category: 'Performance',
      strategy: 'quarter',
    },
    {
      metricId: 'net_utility_operating_income',
      category: 'Performance',
      strategy: 'quarter',
      secondaryMetricIds: ['operating_margin_pct'],
    },
  ],
  'Liquids pipeline': [
    {
      metricId: 'liq_operating_revenue',
      category: 'Performance',
      strategy: 'quarter',
    },
    {
      metricId: 'liq_net_carrier_operating_income',
      category: 'Performance',
      strategy: 'quarter',
      secondaryMetricIds: ['liq_operating_margin_pct'],
    },
    {
      metricId: 'liq_barrels_delivered',
      category: 'Activity',
      strategy: 'quarter',
    },
    {
      metricId: 'p700_interstate_operating_revenue',
      label: 'Page 700 revenue vs cost of service',
      category: 'Regulation',
      strategy: 'annual',
      secondaryMetricIds: [
        'p700_total_cost_of_service',
        'p700_revenue_to_cost_ratio',
        'p700_revenue_less_cost_of_service',
      ],
    },
  ],
  'Intrastate / Hinshaw': [
    {
      metricId: 'i311_billed_transport_usage',
      category: 'Activity',
      strategy: 'quarter',
    },
    {
      metricId: 'i311_firm_share',
      category: 'Commercial',
      strategy: 'quarter',
    },
    {
      metricId: 'i311_top5_shipper_share',
      category: 'Commercial',
      strategy: 'quarter',
    },
    {
      metricId: 'i311_affiliate_activity',
      category: 'Commercial',
      strategy: 'quarter',
    },
    {
      metricId: 'i311_annual_transport_revenue',
      category: 'Performance',
      strategy: 'annual',
    },
    {
      metricId: 'i311_reporting_state',
      category: 'Status',
      strategy: 'quarter',
      fallbackOnly: true,
    },
  ],
  'LNG facility': [
    {
      metricId: 'lng_liquefaction_capacity',
      category: 'Profile',
      strategy: 'single-series',
    },
    {
      metricId: 'lng_status_operating',
      category: 'Status',
      strategy: 'latest-event',
    },
    {
      metricId: 'lng_status_authorised',
      category: 'Status',
      strategy: 'latest-event',
    },
    {
      metricId: 'lng_status_commissioning',
      category: 'Status',
      strategy: 'latest-event',
    },
    {
      metricId: 'lng_status_requested',
      category: 'Status',
      strategy: 'latest-event',
    },
    {
      metricId: 'lng_operational_report',
      category: 'Status',
      strategy: 'latest-event',
    },
    {
      metricId: 'lng_inspection',
      category: 'Regulation',
      strategy: 'latest-event',
    },
  ],
};

const pointValue = (point: BackendObservation) =>
  selectBackendDisplayValue(point.value);

export function isSafeKeyMetricPoint(
  metric: BackendMetricSeries,
  point: BackendObservation,
) {
  if (
    point.quality.availability !== 'present' ||
    point.quality.validation !== 'pass' ||
    point.quality.version_status === 'superseded' ||
    !point.scope.resolved ||
    point.source.canonical === false ||
    isMetricPresentationBlocked(
      metric.id,
      point.value.display_unit || point.value.unit,
      point.quality.validation,
    )
  ) {
    return false;
  }
  const value = pointValue(point);
  if (typeof value === 'number') return Number.isFinite(value);
  return (
    typeof value === 'string' &&
    value.trim().length > 0 &&
    value.length <= 160 &&
    !/[\r\n]/.test(value) &&
    !isStructuredFercValue(value)
  );
}

const comparableUnit = (
  metric: BackendMetricSeries,
  point: BackendObservation,
) =>
  unitLabel(
    presentationUnit(
      metric.id,
      point.value.display_unit || point.value.unit,
      {
        configuredDisplayUnit: metric.configuredDisplayUnit,
        displayScale: point.value.display_scale,
        origin: point.quality.origin,
        validation: point.quality.validation,
      },
    ),
  )
    .trim()
    .toLowerCase();

export const observationSeriesKey = (point: BackendObservation) =>
  point.comparison?.series_id ||
  [
    point.scope?.actual,
    point.period?.basis,
    point.value?.display_unit || point.value?.unit,
  ]
    .filter(Boolean)
    .join('|');

const normalizedSeriesKey = (
  metric: BackendMetricSeries,
  point: BackendObservation,
) =>
  [point.scope.actual, point.period.basis, comparableUnit(metric, point)].join(
    '|',
  );

const equivalentValueKey = (
  metric: BackendMetricSeries,
  point: BackendObservation,
) => JSON.stringify([pointValue(point), comparableUnit(metric, point)]);

function pointAtLatestPeriod(
  metric: BackendMetricSeries,
  points: BackendObservation[],
) {
  const latestSortKey = points.map((point) => point.sortKey).sort().at(-1);
  const latest = points.filter((point) => point.sortKey === latestSortKey);
  if (new Set(latest.map((point) => equivalentValueKey(metric, point))).size > 1)
    return null;
  return (
    latest.find((point) => point.source.canonical === true) ||
    latest.at(-1) ||
    null
  );
}

function selectedSeries(
  metric: BackendMetricSeries,
  basis?: string,
): { point: BackendObservation; points: BackendObservation[] } | null {
  const candidates = metric.points.filter(
    (point) =>
      isSafeKeyMetricPoint(metric, point) &&
      (!basis || point.period.basis === basis),
  );
  const groups = new Map<string, BackendObservation[]>();
  for (const point of candidates) {
    const key = normalizedSeriesKey(metric, point);
    groups.set(key, [...(groups.get(key) || []), point]);
  }
  if (groups.size !== 1) return null;
  const points = [...groups.values()][0];
  const point = pointAtLatestPeriod(metric, points);
  return point ? { point, points } : null;
}

function latestEventPoint(metric: BackendMetricSeries) {
  const candidates = metric.points.filter((point) =>
    isSafeKeyMetricPoint(metric, point),
  );
  return pointAtLatestPeriod(metric, candidates);
}

const quarterIdentity = (point: BackendObservation) => {
  const match = /^(\d{4})Q([1-4])$/.exec(point.period.label || '');
  return match
    ? { year: Number(match[1]), quarter: Number(match[2]) }
    : null;
};

function priorYearQuarter(
  metric: BackendMetricSeries,
  point: BackendObservation,
  points: BackendObservation[],
) {
  if (typeof pointValue(point) !== 'number') return null;
  const identity = quarterIdentity(point);
  if (!identity) return null;
  const prior = points.filter((candidate) => {
    const candidateIdentity = quarterIdentity(candidate);
    return (
      typeof pointValue(candidate) === 'number' &&
      candidateIdentity?.year === identity.year - 1 &&
      candidateIdentity.quarter === identity.quarter
    );
  });
  return pointAtLatestPeriod(metric, prior);
}

function selectPoint(
  metric: BackendMetricSeries,
  strategy: KeyMetricStrategy,
) {
  if (strategy === 'latest-event') {
    return { point: latestEventPoint(metric), points: [] };
  }
  const basis =
    strategy === 'quarter'
      ? 'quarter'
      : strategy === 'annual'
        ? 'annual'
        : strategy === 'snapshot'
          ? 'snapshot'
          : undefined;
  return selectedSeries(metric, basis) || { point: null, points: [] };
}

export function keyMetricDefinitions(regime: string) {
  return templates[regime] || [];
}

export function selectAssetKeyMetrics(
  asset: KeyMetricAssetContext,
  metrics: BackendMetricSeries[],
  limit = 6,
) {
  if (limit <= 0) return [];
  const definitions = keyMetricDefinitions(asset.regime);
  if (definitions.length === 0) return [];
  const byId = new Map(metrics.map((metric) => [metric.id, metric]));
  const selected: SelectedKeyMetric[] = [];
  for (const definition of definitions) {
    if (definition.fallbackOnly && selected.length > 0) continue;
    if (
      definition.strategy === 'latest-event' &&
      asset.scopeRelation === 'shared_filer_entity_context'
    ) {
      continue;
    }
    const metric = byId.get(definition.metricId);
    if (!metric) continue;
    const { point, points } = selectPoint(metric, definition.strategy);
    if (!point) continue;
    selected.push({
      definition,
      metric,
      point,
      priorYearPoint:
        definition.strategy === 'quarter'
          ? priorYearQuarter(metric, point, points)
          : null,
      secondary: (definition.secondaryMetricIds || []).flatMap(
        (secondaryMetricId) => {
          const secondaryMetric = byId.get(secondaryMetricId);
          if (!secondaryMetric) return [];
          const selectedSecondary = selectPoint(
            secondaryMetric,
            definition.strategy,
          ).point;
          return selectedSecondary &&
            selectedSecondary.period.label === point.period.label
            ? [{ metric: secondaryMetric, point: selectedSecondary }]
            : [];
        },
      ),
    });
    if (selected.length === limit) break;
  }
  return selected;
}
