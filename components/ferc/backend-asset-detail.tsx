'use client';

import { useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  CircleAlert,
  Database,
  FileText,
  GitCompareArrows,
  Info,
} from 'lucide-react';
import {
  CartesianGrid,
  Line,
  LineChart,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { ChartContainer } from '@/components/ui/chart';
import {
  comparisonUnitLabel,
  formatBackendValue,
  formatComparisonValue,
  formatDate,
  humanizeFercReason,
  humanizeFercText,
  isMetricPresentationBlocked,
  isStructuredFercValue,
  presentationUnit,
  presentationUnitOverrideNote,
  selectBackendDisplayValue,
  sourceUnitLabel,
  unitLabel,
} from '@/lib/ferc/format';
import {
  keyMetricScopeLabel,
  observationSeriesKey,
  selectAssetKeyMetrics,
  isSafeKeyMetricPoint,
} from '@/lib/ferc/key-metrics';
import { uniqueObservationPeriods } from '@/lib/ferc/observations';
import {
  metricPresentation,
  observationFiledValues,
} from '@/lib/ferc/metric-presentation';
import type {
  BackendMetricSeries,
  BackendObservation,
  OperatingAssetDetail,
  SourceDetail,
} from '@/lib/ferc/types';

const statusLabel = (value: string | null | undefined) =>
  value ? humanizeFercText(value) : 'Not assessed';

export function formatObservationValue(
  point: BackendObservation,
  exact = false,
  metricId?: string | null,
  configuredDisplayUnit?: string | null,
) {
  const suppliedUnit = point.value.display_unit || point.value.unit;
  if (
    isMetricPresentationBlocked(
      metricId,
      suppliedUnit,
      point.quality.validation,
    )
  ) {
    return 'Unit conflict — see evidence';
  }
  return formatBackendValue(
    selectBackendDisplayValue(point.value),
    presentationUnit(metricId, suppliedUnit, {
      configuredDisplayUnit,
      displayScale: point.value.display_scale,
      origin: point.quality.origin,
      validation: point.quality.validation,
    }),
    exact,
  );
}

const periodLabel = (point: BackendObservation) =>
  point.period?.label ||
  point.period?.instant ||
  point.period?.end ||
  point.sortKey ||
  'Unlabelled period';

function metricSeriesGroups(metric: BackendMetricSeries | undefined) {
  const groups = new Map<string, BackendObservation[]>();
  for (const point of metric?.points || []) {
    const key = observationSeriesKey(point);
    groups.set(key, [...(groups.get(key) || []), point]);
  }
  const usableCount = (points: BackendObservation[]) =>
    points.filter(
      (point) =>
        point.quality.availability === 'present' &&
        point.quality.validation === 'pass' &&
        point.quality.version_status !== 'superseded',
    ).length;
  return [...groups.entries()].sort((left, right) => {
    const usableDifference = usableCount(right[1]) - usableCount(left[1]);
    if (usableDifference) return usableDifference;
    return right[1].length - left[1].length || left[0].localeCompare(right[0]);
  });
}

const recordText = (record: Record<string, unknown>, key: string) => {
  const value = record[key];
  return typeof value === 'string' || typeof value === 'number'
    ? String(value)
    : undefined;
};

const recordNumber = (record: Record<string, unknown>, key: string) => {
  const value = record[key];
  if (typeof value === 'number') return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
};

const recordTextList = (record: Record<string, unknown>, key: string) => {
  const value = record[key];
  if (Array.isArray(value)) return value.map(String);
  if (typeof value !== 'string' || !value) return undefined;
  try {
    const parsed: unknown = JSON.parse(value);
    if (Array.isArray(parsed)) return parsed.map(String);
  } catch {
    // Some exports use a single filing ID instead of a JSON list.
  }
  return [value];
};

export function safeFercUrl(raw: string | null | undefined) {
  if (!raw) return undefined;
  try {
    const value = new URL(raw);
    const placeholder =
      /^(?:<[^>]*>|redacted|placeholder|your[ _-]*(?:api[ _-]*)?key|change[ _-]*me|\*+)$/i;
    const hasPlaceholderCredential = [...value.searchParams].some(
      ([key, credential]) =>
        /(?:api[ _-]*key|access[ _-]*token|token|secret)/i.test(key) &&
        (!credential || placeholder.test(credential.trim())),
    );
    if (
      !hasPlaceholderCredential &&
      !/<redacted>|%3credacted%3e/i.test(raw) &&
      value.protocol === 'https:' &&
      (value.hostname === 'ferc.gov' ||
        value.hostname.endsWith('.ferc.gov') ||
        value.hostname === 'govinfo.gov' ||
        value.hostname === 'www.govinfo.gov')
    ) {
      return value.toString();
    }
  } catch {
    // Invalid or non-public source URLs are intentionally omitted.
  }
  return undefined;
}

function observationWarnings(point: BackendObservation) {
  const warnings = [
    point.quality.validation !== 'pass'
      ? `Validation: ${statusLabel(point.quality.validation)}.`
      : null,
    point.quality.review_status
      ? `Review status: ${statusLabel(point.quality.review_status)}.`
      : null,
    point.quality.missing_reason,
    point.quality.qa_flags,
    point.source.canonical === false
      ? 'This source occurrence is not the canonical filing occurrence.'
      : null,
    point.scope.resolved
      ? null
      : 'The backend did not resolve this observation scope.',
    point.quality.version_status === 'superseded'
      ? 'This filing occurrence is superseded and is excluded from current values and charts.'
      : null,
    ...(point.comparison?.reasons || []).map(humanizeFercReason),
    point.notes,
    ...point.source.assertions.flatMap((assertion) =>
      [assertion.reviewState, assertion.reviewerNote].filter(Boolean),
    ),
  ].filter((warning): warning is string => Boolean(warning));
  return [...new Set(warnings.map(humanizeFercText))];
}

export function observationSource(
  asset: Pick<OperatingAssetDetail['asset'], 'name'>,
  metric: BackendMetricSeries,
  point: BackendObservation,
): SourceDetail {
  const dates = point.dates || {};
  const edgeInputs = (point.lineage?.edgeSample || []).map((edge, index) => {
    const role =
      typeof edge.input_role === 'string'
        ? edge.input_role
        : typeof edge.role === 'string'
          ? edge.role
          : `Derivation input ${index + 1}`;
    const rawValue = edge.input_value;
    const rawUnit = edge.input_unit;
    const value =
      typeof rawValue === 'string' || typeof rawValue === 'number'
        ? formatBackendValue(
            rawValue,
            typeof rawUnit === 'string' ? rawUnit : null,
            true,
          )
        : typeof rawUnit === 'string'
          ? `Unit: ${unitLabel(rawUnit)}`
          : '';
    return {
      label: role,
      value: value || 'Referenced input',
      period:
        typeof edge.input_period === 'string' ? edge.input_period : undefined,
      sourceId:
        typeof edge.input_observation_id === 'string'
          ? edge.input_observation_id
          : typeof edge.input_source_fact_id === 'string'
            ? edge.input_source_fact_id
            : undefined,
      details: [
        recordText(edge, 'input_source_system')
          ? `Source system: ${recordText(edge, 'input_source_system')}`
          : null,
        recordText(edge, 'input_filing_id')
          ? `Filing: ${recordText(edge, 'input_filing_id')}`
          : null,
        recordText(edge, 'input_source_fact_id')
          ? `Source fact: ${recordText(edge, 'input_source_fact_id')}`
          : null,
        recordText(edge, 'input_observation_id')
          ? `Observation: ${recordText(edge, 'input_observation_id')}`
          : null,
        recordText(edge, 'input_context_id')
          ? `Context: ${recordText(edge, 'input_context_id')}`
          : null,
        recordText(edge, 'input_population_id')
          ? `Population: ${recordText(edge, 'input_population_id')}`
          : null,
        recordText(edge, 'input_concept')
          ? `Concept: ${recordText(edge, 'input_concept')}`
          : null,
        recordText(edge, 'operator_sign') || recordText(edge, 'coefficient')
          ? `Operation: ${recordText(edge, 'operator_sign') || '·'} ${recordText(edge, 'coefficient') || '1'}`
          : null,
        recordText(edge, 'input_version_status')
          ? `Version: ${recordText(edge, 'input_version_status')}`
          : null,
      ].filter((detail): detail is string => Boolean(detail)),
    };
  });
  const populations = (point.lineage?.populationSample || []).map(
    (population, index) => {
      const aggregateValue = recordText(population, 'aggregate_value');
      const aggregateUnit = recordText(population, 'aggregate_unit');
      const aggregate = aggregateValue
        ? formatBackendValue(aggregateValue, aggregateUnit, true)
        : '';
      return {
        id:
          recordText(population, 'population_id') ||
          `Population sample ${index + 1}`,
        source: [
          recordText(population, 'source_system'),
          recordText(population, 'source_table'),
        ]
          .filter(Boolean)
          .join(' · '),
        filingIds: recordTextList(population, 'filing_ids'),
        inclusionRule: recordText(population, 'inclusion_rule'),
        exclusionRule: recordText(population, 'exclusion_rule'),
        rowCount: recordNumber(population, 'row_count'),
        candidateCount: recordNumber(population, 'candidate_count'),
        excludedCount: recordNumber(population, 'excluded_count'),
        memberDigest: recordText(population, 'member_digest'),
        aggregate: aggregate || undefined,
        note:
          recordText(population, 'note') ||
          recordText(population, 'empty_reason'),
      };
    },
  );
  const firstAssertion = point.source.assertions[0];
  const lineageWarnings = [];
  if (point.lineage && !point.lineage.edgeSampleComplete) {
    lineageWarnings.push(
      `Showing ${point.lineage.edgeSample.length} of ${point.lineage.edgeCount} lineage edges.`,
    );
  }
  if (point.lineage && !point.lineage.populationSampleComplete) {
    lineageWarnings.push(
      `Showing a bounded population sample; the backend records ${point.lineage.populationCount} population descriptor(s).`,
    );
  }
  const { storedValue, filedValue: rawFiledValue } =
    observationFiledValues(point);
  const structuredFiledValue = isStructuredFercValue(rawFiledValue);
  const suppliedUnit = point.source.fact?.unitText || point.value.unit;
  const unitPresentationBlocked = isMetricPresentationBlocked(
    metric.id,
    point.value.display_unit || point.value.unit,
    point.quality.validation,
  );
  const presentationNote = presentationUnitOverrideNote(
    metric.id,
    point.value.display_unit || point.value.unit,
    {
      configuredDisplayUnit: metric.configuredDisplayUnit,
      displayScale: point.value.display_scale,
      origin: point.quality.origin,
      validation: point.quality.validation,
    },
  );
  return {
    id: point.id,
    title: `${asset.name} · ${metricPresentation(metric, [point]).label}`,
    sourceSystem: point.source.system || point.sourceRegime || 'FERC',
    nativeIdentity: [
      point.source.filingRef,
      point.source.documentId,
      point.source.sourceFactId,
      ...point.source.assertions.map((assertion) => assertion.id),
    ]
      .filter(Boolean)
      .join(' · '),
    accession: point.source.accession || undefined,
    filingVersion: [point.source.versionStatus, point.quality.version_status]
      .filter(Boolean)
      .join(' · '),
    canonicalStatus:
      point.source.canonical === null
        ? 'Unknown'
        : point.source.canonical
          ? 'Canonical occurrence'
          : 'Noncanonical occurrence',
    canonicalReason: point.source.canonicalReason || undefined,
    acceptanceStatus: point.source.acceptanceStatus || undefined,
    dataOrigin: point.source.dataOrigin || undefined,
    period: periodLabel(point),
    scope: [
      point.scope.actual,
      point.scope.contract_rule
        ? `Contract: ${point.scope.contract_rule}`
        : null,
    ]
      .filter(Boolean)
      .join(' · '),
    unit: unitPresentationBlocked
      ? 'Unit conflict — see warnings'
      : unitLabel(
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
        ),
    displayUnit: unitPresentationBlocked
      ? 'Unit conflict — see warnings'
      : unitLabel(
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
        ),
    filedUnit: sourceUnitLabel(suppliedUnit),
    displayScale: point.value.display_scale,
    value: formatObservationValue(
      point,
      true,
      metric.id,
      metric.configuredDisplayUnit,
    ),
    valueLabel: 'Displayed value',
    storedValue: storedValue !== rawFiledValue ? storedValue : undefined,
    filedValue: rawFiledValue
      ? structuredFiledValue
        ? formatBackendValue(rawFiledValue, null, true)
        : rawFiledValue
      : undefined,
    filedValueRaw: structuredFiledValue ? rawFiledValue : undefined,
    comparisonValue:
      metric.id !== 'certificated_horsepower' &&
      typeof point.comparison?.comparison_value_base === 'number'
        ? formatComparisonValue(
            point.comparison.comparison_value_base,
            point.comparison.base_unit_family,
            true,
            metric.id,
          )
        : undefined,
    comparisonValueLabel:
      point.comparison?.base_unit_family &&
      metric.id !== 'certificated_horsepower'
        ? `Backend comparison base · ${comparisonUnitLabel(point.comparison.base_unit_family, metric.id)} · no frontend conversion`
        : undefined,
    method: statusLabel(point.quality.method),
    availability: statusLabel(point.quality.availability),
    origin: statusLabel(point.quality.origin),
    validation: statusLabel(point.quality.validation),
    description:
      firstAssertion?.verbatimSpan ||
      firstAssertion?.scopeNote ||
      metricPresentation(metric, [point]).description ||
      undefined,
    formula: point.lineage?.derivation || undefined,
    inputs: edgeInputs.length ? edgeInputs : undefined,
    populations: populations.length ? populations : undefined,
    warnings: [
      ...new Set(
        [
          ...observationWarnings(point),
          ...lineageWarnings,
          presentationNote,
          unitPresentationBlocked
            ? 'Unit conflict: this barrel-mile metric carries a barrel source tag. No presentation unit is asserted; inspect the filed unit and evidence.'
            : null,
        ].filter((warning): warning is string => Boolean(warning)),
      ),
    ],
    url: safeFercUrl(point.source.url),
    filingId: point.source.filingId || undefined,
    sourceFactId: point.source.sourceFactId || undefined,
    documentId: point.source.documentId || undefined,
    reportingDate:
      dates.source_reporting_end || dates.source_reporting_instant || undefined,
    filedDate: dates.source_filed || undefined,
    postedDate: dates.source_posted || undefined,
    issuedDate: dates.source_issued || undefined,
    effectiveDate: dates.regulatory_effective || undefined,
    retrievedDate: dates.retrieved || undefined,
    firstSeen: dates.first_seen || undefined,
    lineagePath: point.lineage?.fullPath || undefined,
    lineageObservationId: point.lineage?.fullPath ? point.id : undefined,
  };
}

function qualityTone(point: BackendObservation) {
  return point.quality.availability === 'present' &&
    point.quality.validation === 'pass' &&
    point.quality.version_status !== 'superseded'
    ? 'good'
    : 'warn';
}

export function BackendAssetDetail({
  detail,
  companyContext,
  back,
  openCompare,
  openSource,
  routeWarnings = [],
}: {
  detail: OperatingAssetDetail;
  companyContext: string;
  back: () => void;
  openCompare: () => void;
  openSource: (source: SourceDetail) => void;
  routeWarnings?: string[];
}) {
  const { asset } = detail;
  const metrics = detail.metrics.filter((metric) => metric.pointCount > 0);
  const keyMetrics = selectAssetKeyMetrics(asset, metrics);
  const initialMetric =
    keyMetrics[0]?.metric ||
    metrics.find((metric) => metric.presentCount > 0) ||
    metrics[0];
  const [selectedMetricId, setSelectedMetricId] = useState(
    initialMetric?.id || '',
  );
  const selectedMetric =
    metrics.find((metric) => metric.id === selectedMetricId) || initialMetric;
  const groupedSeries = metricSeriesGroups(selectedMetric);
  const [selectedSeriesKey, setSelectedSeriesKey] = useState(
    keyMetrics[0]?.point
      ? observationSeriesKey(keyMetrics[0].point)
      : groupedSeries[0]?.[0] || '',
  );
  const effectiveSeriesKey = groupedSeries.some(
    ([key]) => key === selectedSeriesKey,
  )
    ? selectedSeriesKey
    : groupedSeries[0]?.[0] || '';
  const selectedPoints =
    groupedSeries.find(([key]) => key === effectiveSeriesKey)?.[1] ||
    groupedSeries[0]?.[1] ||
    [];
  const chartSeries = uniqueObservationPeriods(
    selectedPoints.filter(
      (point) =>
        selectedMetric &&
        isSafeKeyMetricPoint(selectedMetric, point) &&
        typeof point.value.display_value === 'number',
    ),
  );
  const chartPoints = chartSeries.points.map((point) => ({
    id: point.id,
    period: periodLabel(point),
    value: point.value.display_value as number,
    point,
  }));
  const sortedEvents = [...detail.events].sort((left, right) =>
    right.date.localeCompare(left.date),
  );
  const latestEvent = sortedEvents[0];
  const structuredInterest = asset.interests
    .map((interest) => {
      const amount =
        interest.pct === null ? interest.qualifier : `${interest.pct}%`;
      return [
        interest.parent,
        amount,
        interest.basis ? `${statusLabel(interest.basis)} basis` : null,
        interest.ticker || null,
      ]
        .filter(Boolean)
        .join(' · ');
    })
    .join(' / ');
  const additionalInterest =
    asset.interestDisplay && !structuredInterest.includes(asset.interestDisplay)
      ? asset.interestDisplay
      : null;
  const observationCount = asset.dataSummary?.observations ?? 0;
  const usableCount = asset.dataSummary?.usable ?? 0;
  const qualityFlagCount = asset.qualityFlagCount ?? asset.reviewCount;
  const openReviewCount = asset.openReviewCount ?? 0;
  const reviewTone =
    observationCount === 0 || openReviewCount > 0 || usableCount === 0
      ? 'warn'
      : qualityFlagCount > 0
        ? 'neutral'
        : 'good';

  return (
    <main className="page-shell detail-page backend-detail-page">
      <Link
        className="back-button"
        href={`?view=assets${companyContext === 'All companies' ? '' : `&company=${encodeURIComponent(companyContext)}`}`}
        prefetch={false}
        onClick={(event) => {
          if (
            !event.metaKey &&
            !event.ctrlKey &&
            !event.shiftKey &&
            !event.altKey
          ) {
            event.preventDefault();
            back();
          }
        }}
      >
        <ArrowLeft /> Back to Operating Assets
      </Link>
      <header className="detail-header">
        <div>
          <div className="detail-badges">
            <span className="status status-neutral">{asset.regime}</span>
            <span className={`status status-${reviewTone}`}>
              {observationCount === 0
                ? 'Identity only'
                : openReviewCount > 0
                  ? `${openReviewCount} need review`
                  : qualityFlagCount > 0
                    ? `${qualityFlagCount} quality flags`
                    : usableCount > 0
                      ? 'Validated records available'
                      : 'No usable records'}
            </span>
            {!asset.inScope && (
              <span className="status status-warn">
                Outside current template
              </span>
            )}
          </div>
          <h1>{asset.name}</h1>
          <p>
            {asset.company} ({asset.ticker}) · {asset.legalFiler} ·{' '}
            {asset.cid || 'No FERC CID'}
          </p>
          <details className="asset-context-details">
            <summary>Ownership and reporting details</summary>
            {structuredInterest && (
              <p className="asset-ownership">
                Structured ownership record: {structuredInterest}
              </p>
            )}
            {additionalInterest && (
              <p className="asset-ownership asset-interest-disclosure">
                Additional asset-interest disclosure: {additionalInterest}
              </p>
            )}
            {asset.note && (
              <p className="asset-directory-note">
                <strong>Directory note:</strong> {asset.note}
              </p>
            )}
            <p>{asset.scopeNote}</p>
          </details>
        </div>
        <div className="detail-header-actions">
          <button
            className="compare-button"
            onClick={openCompare}
            disabled={!asset.comparisonEligible}
            title={
              asset.comparisonBlockedReason
                ? statusLabel(asset.comparisonBlockedReason)
                : undefined
            }
          >
            <GitCompareArrows /> Compare
          </button>
          <small>Data as of {formatDate(detail.asOf)}</small>
        </div>
      </header>

      {metrics.length > 0 && (
        <section
          className={`key-metrics-section${keyMetrics.length === 0 ? ' key-metrics-empty' : ''}`}
          aria-labelledby="asset-key-metrics-title"
        >
          <div className="key-metrics-heading">
            <div>
              <p className="eyebrow">At a glance</p>
              <h2 id="asset-key-metrics-title">Key metrics</h2>
              <p>
                {asset.scopeRelation === 'shared_filer_entity_context'
                  ? `${asset.legalFiler} figures; not allocated to this individual asset. `
                  : 'Filed-entity figures; see each value’s evidence for its exact scope. '}
                Each value shows its own reporting period.
              </p>
            </div>
            <a href="#metric-explorer-title">
              Browse all {metrics.length.toLocaleString()} metrics{' '}
              <ArrowRight />
            </a>
          </div>
          {keyMetrics.length > 0 ? (
            <div className="key-metrics-grid">
              {keyMetrics.map(
                ({ definition, metric, point, priorYearPoint, secondary }) => {
                  const displayValue = selectBackendDisplayValue(point.value);
                  const isTextValue =
                    typeof displayValue === 'string' &&
                    !/^[+-]?(?:\d+(?:,\d{3})*|\d*\.\d+)(?:e[+-]?\d+)?$/i.test(
                      displayValue.trim(),
                    );
                  return (
                    <article
                      className={`key-metric-card${isTextValue ? ' key-metric-text' : ''}`}
                      key={metric.id}
                    >
                      <span className="key-metric-category">
                        {definition.category}
                      </span>
                      <h3>{definition.label || metric.label}</h3>
                      <strong>
                        {formatObservationValue(
                          point,
                          false,
                          metric.id,
                          metric.configuredDisplayUnit,
                        )}
                      </strong>
                      <small>{periodLabel(point)}</small>
                      {definition.strategy === 'latest-event' && (
                        <small className="key-metric-scope">
                          Scope: {keyMetricScopeLabel(point)}
                        </small>
                      )}
                      {secondary.map((item) => (
                        <button
                          type="button"
                          className="key-metric-secondary"
                          key={item.metric.id}
                          onClick={() =>
                            openSource(
                              observationSource(asset, item.metric, item.point),
                            )
                          }
                        >
                          <span>
                            <b>
                              {
                                metricPresentation(item.metric, [item.point])
                                  .label
                              }
                              :
                            </b>{' '}
                            {formatObservationValue(
                              item.point,
                              false,
                              item.metric.id,
                              item.metric.configuredDisplayUnit,
                            )}
                          </span>
                          <span className="key-metric-evidence-label">
                            Evidence <ArrowUpRight />
                          </span>
                        </button>
                      ))}
                      {priorYearPoint && (
                        <button
                          type="button"
                          className="key-metric-comparison"
                          onClick={() =>
                            openSource(
                              observationSource(asset, metric, priorYearPoint),
                            )
                          }
                        >
                          <span>
                            Prior year:{' '}
                            {formatObservationValue(
                              priorYearPoint,
                              false,
                              metric.id,
                              metric.configuredDisplayUnit,
                            )}{' '}
                            in {periodLabel(priorYearPoint)}
                          </span>
                          <span className="key-metric-evidence-label">
                            Evidence <ArrowUpRight />
                          </span>
                        </button>
                      )}
                      <button
                        type="button"
                        className="key-metric-action"
                        onClick={() =>
                          openSource(observationSource(asset, metric, point))
                        }
                      >
                        Evidence for {metricPresentation(metric, [point]).label}{' '}
                        <ArrowUpRight />
                      </button>
                    </article>
                  );
                },
              )}
            </div>
          ) : (
            <p className="key-metrics-empty-copy">
              A reliable key-metric summary is not available for this asset. You
              can still browse its source records below.
            </p>
          )}
        </section>
      )}

      {routeWarnings.length > 0 && (
        <output className="comparison-warning compact-warning">
          <CircleAlert />
          <div>
            <strong>Comparison could not be restored</strong>
            {routeWarnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </output>
      )}
      {!asset.comparisonEligible &&
        asset.comparisonBlockedReason &&
        routeWarnings.length === 0 && (
          <p className="asset-comparison-note">
            Comparison unavailable:{' '}
            {humanizeFercReason(asset.comparisonBlockedReason)}
          </p>
        )}
      <details className="asset-coverage-details">
        <summary>
          Data coverage: {usableCount.toLocaleString()} usable records ·{' '}
          {metrics.length} metrics
        </summary>
        <p>
          {observationCount.toLocaleString()} observations · Latest available
          period {formatDate(asset.latestPeriod ?? undefined)} · Latest FERC
          filing {asset.lastFiled ? formatDate(asset.lastFiled) : 'Not mapped'}
        </p>
      </details>

      <section className="what-changed backend-feed-state">
        <div>
          <p className="eyebrow">Current operating feed</p>
          <h2>No current investor events in this generation</h2>
          <p>
            All {detail.events.length.toLocaleString()} linked events are
            historical backfill or data-quality events. They remain in the
            archive and are not promoted as new developments.
          </p>
        </div>
        {latestEvent && (
          <button
            className="source-button"
            onClick={() => openSource(latestEvent.source)}
          >
            Latest archive evidence <ArrowUpRight />
          </button>
        )}
      </section>

      {metrics.length > 0 ? (
        <section
          className="section-block backend-metric-browser"
          aria-labelledby="metric-explorer-title"
        >
          <div className="section-title metric-browser-title">
            <div>
              <p className="eyebrow">Source-backed history</p>
              <h2 id="metric-explorer-title" tabIndex={-1}>
                Metric explorer
              </h2>
              <p>
                Values retain the backend’s period, unit, quality, scope and
                source identity.
              </p>
            </div>
            <label>
              <span>Metric</span>
              <select
                value={selectedMetric?.id || ''}
                onChange={(event) => {
                  const nextId = event.target.value;
                  const nextMetric = metrics.find(
                    (metric) => metric.id === nextId,
                  );
                  const nextGroups = metricSeriesGroups(nextMetric);
                  setSelectedMetricId(nextId);
                  setSelectedSeriesKey(nextGroups[0]?.[0] || '');
                }}
              >
                {metrics.map((metric) => (
                  <option key={metric.id} value={metric.id}>
                    {metricPresentation(metric).label} ({metric.presentCount}/
                    {metric.pointCount})
                  </option>
                ))}
              </select>
            </label>
          </div>
          {selectedMetric && (
            <>
              <div className="metric-definition">
                <div>
                  <strong>
                    {metricPresentation(selectedMetric, selectedPoints).label}
                  </strong>
                  <p>
                    {metricPresentation(selectedMetric, selectedPoints)
                      .description || 'No registry definition supplied.'}
                  </p>
                </div>
                <span>
                  {selectedMetric.presentCount} present ·{' '}
                  {selectedMetric.openReviewCount > 0
                    ? `${selectedMetric.openReviewCount} need review`
                    : selectedMetric.reviewCount > 0
                      ? `${selectedMetric.reviewCount} quality flags`
                      : 'no open reviews'}
                </span>
              </div>
              {groupedSeries.length > 1 && (
                <label className="series-selector">
                  <span>Filed scope series</span>
                  <select
                    value={effectiveSeriesKey}
                    onChange={(event) =>
                      setSelectedSeriesKey(event.target.value)
                    }
                  >
                    {groupedSeries.map(([key, points], index) => (
                      <option key={key} value={key}>
                        Series {index + 1} · {points[0]?.period.basis} ·{' '}
                        {isMetricPresentationBlocked(
                          selectedMetric.id,
                          points[0]?.value.display_unit ||
                            points[0]?.value.unit,
                          points[0]?.quality.validation,
                        )
                          ? 'Unit conflict'
                          : unitLabel(
                              presentationUnit(
                                selectedMetric.id,
                                points[0]?.value.display_unit ||
                                  points[0]?.value.unit,
                                {
                                  configuredDisplayUnit:
                                    selectedMetric.configuredDisplayUnit,
                                  displayScale: points[0]?.value.display_scale,
                                  origin: points[0]?.quality.origin,
                                  validation: points[0]?.quality.validation,
                                },
                              ),
                            )}{' '}
                        · {points.length} records
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {selectedPoints[0] && (
                <div className="metric-scope-note">
                  <Info />
                  <p>
                    <strong>Actual scope:</strong>{' '}
                    {selectedPoints[0].scope.actual}
                  </p>
                </div>
              )}
              {(chartSeries.duplicateCount > 0 ||
                chartSeries.conflictCount > 0) && (
                <p className="chart-record-note">
                  {chartSeries.duplicateCount > 0 &&
                    `${chartSeries.duplicateCount} duplicate source occurrence(s) shown once in the chart. `}
                  {chartSeries.conflictCount > 0 &&
                    `${chartSeries.conflictCount} ambiguous period(s) withheld from the trend. `}
                  All source records remain in the table below.
                </p>
              )}
              {chartPoints.length > 1 && chartSeries.conflictCount === 0 ? (
                <ChartContainer
                  config={{
                    value: {
                      label: metricPresentation(selectedMetric, selectedPoints)
                        .label,
                      color: '#176b61',
                    },
                  }}
                  className="backend-metric-chart"
                >
                  <LineChart
                    data={chartPoints}
                    margin={{ left: 8, right: 24, top: 18, bottom: 4 }}
                  >
                    <CartesianGrid vertical={false} />
                    <XAxis dataKey="period" />
                    <YAxis
                      width={72}
                      tickFormatter={(value) =>
                        formatBackendValue(
                          Number(value),
                          presentationUnit(
                            selectedMetric.id,
                            selectedPoints[0]?.value.display_unit ||
                              selectedPoints[0]?.value.unit,
                            {
                              configuredDisplayUnit:
                                selectedMetric.configuredDisplayUnit,
                              displayScale:
                                selectedPoints[0]?.value.display_scale,
                              origin: selectedPoints[0]?.quality.origin,
                              validation: selectedPoints[0]?.quality.validation,
                            },
                          ),
                        )
                      }
                    />
                    <RechartsTooltip
                      formatter={(value) =>
                        formatBackendValue(
                          Number(value),
                          presentationUnit(
                            selectedMetric.id,
                            selectedPoints[0]?.value.display_unit ||
                              selectedPoints[0]?.value.unit,
                            {
                              configuredDisplayUnit:
                                selectedMetric.configuredDisplayUnit,
                              displayScale:
                                selectedPoints[0]?.value.display_scale,
                              origin: selectedPoints[0]?.quality.origin,
                              validation: selectedPoints[0]?.quality.validation,
                            },
                          ),
                          true,
                        )
                      }
                    />
                    <Line
                      dataKey="value"
                      stroke="var(--color-value)"
                      strokeWidth={2.5}
                      connectNulls={false}
                      dot={{ r: 3 }}
                    />
                  </LineChart>
                </ChartContainer>
              ) : (
                <div className="chart-empty">
                  {chartSeries.conflictCount > 0
                    ? 'This series has conflicting source records for the same period, so a trend is not asserted.'
                    : 'This series has fewer than two usable numeric periods.'}{' '}
                  All records remain below with their quality and evidence.
                </div>
              )}
              <details
                className="data-table backend-data-table"
                open={chartPoints.length <= 1 || chartSeries.conflictCount > 0}
              >
                <summary>
                  View {selectedPoints.length} underlying records
                </summary>
                <div className="data-table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Period</th>
                        <th>Value</th>
                        <th>Availability</th>
                        <th>Validation</th>
                        <th>Version</th>
                        <th>Evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedPoints.map((point) => (
                        <tr key={point.id}>
                          <td>{periodLabel(point)}</td>
                          <td>
                            {formatObservationValue(
                              point,
                              true,
                              selectedMetric.id,
                              selectedMetric.configuredDisplayUnit,
                            )}
                          </td>
                          <td>{statusLabel(point.quality.availability)}</td>
                          <td>
                            <span
                              className={`status status-${qualityTone(point)}`}
                            >
                              {statusLabel(point.quality.validation)}
                              {point.quality.version_status === 'superseded'
                                ? ' · superseded'
                                : ''}
                            </span>
                          </td>
                          <td>{statusLabel(point.quality.version_status)}</td>
                          <td>
                            <button
                              onClick={() =>
                                openSource(
                                  observationSource(
                                    asset,
                                    selectedMetric,
                                    point,
                                  ),
                                )
                              }
                            >
                              Details
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          )}
        </section>
      ) : (
        <section className="section-block identity-only-state">
          <Database />
          <div>
            <p className="eyebrow">Availability</p>
            <h2>No mapped operating observations</h2>
            <p>
              {asset.exclusionReason ||
                'The reviewed directory preserves this asset identity without inventing a FERC filing history.'}
            </p>
          </div>
        </section>
      )}

      {detail.annotations.length > 0 && (
        <section className="section-block compact backend-review-notes">
          <p className="eyebrow">Human review</p>
          <h2>Reviewed source annotations</h2>
          <div className="review-note-list">
            {detail.annotations.map((annotation) => (
              <article
                key={`${annotation.source_fact_id}-${annotation.metric_id}`}
              >
                <CircleAlert />
                <div>
                  <strong>
                    {statusLabel(annotation.metric_id)} ·{' '}
                    {statusLabel(annotation.review_status)}
                  </strong>
                  <p>{annotation.rationale}</p>
                  <small>
                    Filed as {annotation.filed_text} · filing{' '}
                    {annotation.filing_id} · reviewed{' '}
                    {formatDate(annotation.reviewed_at)}
                  </small>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {detail.events.length > 0 && (
        <section className="section-block">
          <div className="section-title">
            <div>
              <p className="eyebrow">Historical archive</p>
              <h2>Recent linked filings</h2>
            </div>
          </div>
          <div className="source-list">
            {sortedEvents.slice(0, 8).map((event) => (
              <button key={event.id} onClick={() => openSource(event.source)}>
                <FileText />
                <span>
                  <strong>{event.title}</strong>
                  <small>
                    {formatDate(event.date)} · {event.category}
                  </small>
                </span>
                <ArrowRight />
              </button>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
