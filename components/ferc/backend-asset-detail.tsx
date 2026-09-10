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
import { formatDate } from '@/lib/ferc/format';
import type {
  BackendMetricSeries,
  BackendObservation,
  OperatingAssetDetail,
  SourceDetail,
} from '@/lib/ferc/types';

const statusLabel = (value: string | null | undefined) =>
  value ? value.replaceAll('_', ' ') : 'Not assessed';

const unitLabel = (unit: string | null | undefined) => {
  if (!unit) return '';
  const normalized = unit.toLowerCase();
  if (normalized === 'iso4217:usd') return 'USD';
  if (normalized === 'utr:dth' || normalized === 'ferc:dth') return 'Dth';
  if (normalized === 'utr:bbl') return 'bbl';
  if (normalized === 'utr:mi') return 'miles';
  if (normalized === 'percent') return '%';
  if (normalized === 'fraction') return 'fraction';
  return unit;
};

export function formatBackendValue(
  value: number | string | null | undefined,
  unit?: string | null,
  exact = false,
) {
  if (value === null || value === undefined || value === '')
    return 'Unavailable';
  if (typeof value === 'string')
    return unitLabel(unit) ? `${value} ${unitLabel(unit)}` : value;
  const normalized = unit?.toLowerCase() || '';
  if (normalized.includes('iso4217:usd')) {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      notation: exact ? 'standard' : 'compact',
      maximumFractionDigits: exact ? 2 : 2,
    }).format(value);
  }
  if (normalized === 'percent') return `${value.toFixed(exact ? 4 : 1)}%`;
  const rendered = new Intl.NumberFormat('en-US', {
    notation: exact ? 'standard' : 'compact',
    maximumFractionDigits: exact ? 6 : 2,
  }).format(value);
  const label = unitLabel(unit);
  return label ? `${rendered} ${label}` : rendered;
}

export function formatObservationValue(
  point: BackendObservation,
  exact = false,
) {
  if (
    point.value.display_value !== null &&
    point.value.display_value !== undefined
  ) {
    return formatBackendValue(
      point.value.display_value,
      point.value.display_unit || point.value.unit,
      exact,
    );
  }
  if (point.value.normalized_iso) return point.value.normalized_iso;
  if (point.value.as_filed) return point.value.as_filed;
  return 'Unavailable';
}

const periodLabel = (point: BackendObservation) =>
  point.period?.label ||
  point.period?.instant ||
  point.period?.end ||
  point.sortKey ||
  'Unlabelled period';

const seriesKey = (point: BackendObservation) =>
  point.comparison?.series_id ||
  [
    point.scope?.actual,
    point.period?.basis,
    point.value?.display_unit || point.value?.unit,
  ]
    .filter(Boolean)
    .join('|');

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
    if (
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
  return [
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
    ...(point.comparison?.reasons || []),
    point.notes,
    ...point.source.assertions.flatMap((assertion) =>
      [assertion.reviewState, assertion.reviewerNote].filter(Boolean),
    ),
  ].filter((warning): warning is string => Boolean(warning));
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
    const value = [
      typeof rawValue === 'string' || typeof rawValue === 'number'
        ? String(rawValue)
        : null,
      typeof rawUnit === 'string' ? rawUnit : null,
    ]
      .filter((item): item is string => item !== null)
      .join(' ');
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
      const aggregate = [
        recordText(population, 'aggregate_value'),
        recordText(population, 'aggregate_unit'),
      ]
        .filter(Boolean)
        .join(' ');
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
  return {
    id: point.id,
    title: `${asset.name} · ${metric.label}`,
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
    scope: `${point.scope.actual} · Contract: ${point.scope.contract_rule}`,
    unit: unitLabel(point.value.display_unit || point.value.unit),
    value: formatObservationValue(point, true),
    valueLabel: point.value.as_filed
      ? `Displayed value · filed as ${point.value.as_filed}`
      : 'Displayed value',
    comparisonValue:
      typeof point.comparison?.comparison_value_base === 'number'
        ? formatBackendValue(point.comparison.comparison_value_base, null, true)
        : undefined,
    comparisonValueLabel: point.comparison?.base_unit_family
      ? `Backend comparison base · ${point.comparison.base_unit_family} · no frontend conversion`
      : undefined,
    method: statusLabel(point.quality.method),
    availability: statusLabel(point.quality.availability),
    origin: statusLabel(point.quality.origin),
    validation: statusLabel(point.quality.validation),
    description:
      firstAssertion?.verbatimSpan ||
      firstAssertion?.scopeNote ||
      metric.description ||
      undefined,
    formula: point.lineage?.derivation || undefined,
    inputs: edgeInputs.length ? edgeInputs : undefined,
    populations: populations.length ? populations : undefined,
    warnings: [...observationWarnings(point), ...lineageWarnings],
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
    point.quality.validation === 'pass'
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
  const initialMetric =
    metrics.find((metric) => metric.presentCount > 0) || metrics[0];
  const [selectedMetricId, setSelectedMetricId] = useState(
    initialMetric?.id || '',
  );
  const selectedMetric =
    metrics.find((metric) => metric.id === selectedMetricId) || initialMetric;
  const groupedSeries = (() => {
    const groups = new Map<string, BackendObservation[]>();
    for (const point of selectedMetric?.points || []) {
      const key = seriesKey(point);
      const points = groups.get(key) || [];
      points.push(point);
      groups.set(key, points);
    }
    return [...groups.entries()].sort((left, right) => {
      const present = (entry: [string, BackendObservation[]]) =>
        entry[1].filter((point) => point.quality.availability === 'present')
          .length;
      return present(right) - present(left);
    });
  })();
  const [selectedSeriesKey, setSelectedSeriesKey] = useState(
    groupedSeries[0]?.[0] || '',
  );
  const selectedPoints =
    groupedSeries.find(([key]) => key === selectedSeriesKey)?.[1] ||
    groupedSeries[0]?.[1] ||
    [];
  const chartPoints = selectedPoints
    .filter(
      (point) =>
        point.quality.availability === 'present' &&
        point.quality.validation === 'pass' &&
        point.quality.version_status !== 'superseded' &&
        typeof point.value.display_value === 'number',
    )
    .map((point) => ({
      id: point.id,
      period: periodLabel(point),
      value: point.value.display_value as number,
      point,
    }));
  const headlineMetrics = [
    ...metrics.filter((metric) => metric.role === 'headline' && metric.latest),
    ...metrics.filter((metric) => metric.role !== 'headline' && metric.latest),
  ].slice(0, 4);
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
        interest.ticker ? `${interest.ticker} interest` : null,
      ]
        .filter(Boolean)
        .join(' · ');
    })
    .join(' / ');
  const interestDescription = structuredInterest
    ? asset.interestDisplay &&
      !structuredInterest.includes(asset.interestDisplay)
      ? `${structuredInterest} · ${asset.interestDisplay}`
      : structuredInterest
    : asset.interestDisplay;
  const observationCount = asset.dataSummary?.observations ?? 0;
  const usableCount = asset.dataSummary?.usable ?? 0;
  const reviewTone =
    observationCount === 0 || asset.reviewCount > 0 || usableCount === 0
      ? 'warn'
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
                : asset.reviewCount > 0
                  ? `${asset.reviewCount} review records`
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
          {interestDescription && (
            <p className="asset-ownership">
              Reviewed interest: {interestDescription}
            </p>
          )}
        </div>
        <div className="detail-header-actions">
          <button
            className="compare-button"
            onClick={openCompare}
            disabled={!asset.comparisonEligible}
            title={asset.comparisonBlockedReason || undefined}
          >
            <GitCompareArrows /> Compare
          </button>
          <div className="refresh-card">
            <span>Pinned operating snapshot</span>
            <strong>{formatDate(detail.asOf)}</strong>
            <small>
              History through {formatDate(asset.latestPeriod ?? undefined)}
            </small>
          </div>
        </div>
      </header>
      {routeWarnings.length > 0 && (
        <output className="comparison-warning">
          <CircleAlert />
          <div>
            <strong>Comparison could not be restored</strong>
            {routeWarnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </output>
      )}
      {!asset.comparisonEligible && asset.comparisonBlockedReason && (
        <output className="comparison-warning compact-warning">
          <CircleAlert />
          <div>
            <strong>Comparison unavailable</strong>
            <p>{asset.comparisonBlockedReason}</p>
          </div>
        </output>
      )}
      <div className="scope-warning">
        <Info />
        <div>
          <strong>Reporting scope</strong>
          <p>{asset.scopeNote}</p>
        </div>
      </div>

      <section
        className="backend-summary-strip"
        aria-label="Backend data summary"
      >
        <div>
          <span>Observations</span>
          <strong>
            {asset.dataSummary?.observations.toLocaleString() || 0}
          </strong>
        </div>
        <div>
          <span>Usable</span>
          <strong>{asset.dataSummary?.usable.toLocaleString() || 0}</strong>
        </div>
        <div>
          <span>Metrics</span>
          <strong>
            {asset.dataSummary?.metrics.toLocaleString() || metrics.length}
          </strong>
        </div>
        <div>
          <span>FERC filings</span>
          <strong>
            {asset.lastFiled ? formatDate(asset.lastFiled) : 'Not mapped'}
          </strong>
        </div>
      </section>

      <section className="what-changed backend-feed-state">
        <div>
          <p className="eyebrow">Current operating feed</p>
          <h2>No current investor events in this generation</h2>
          <p>
            All {detail.events.length.toLocaleString()} linked events are
            historical backfill or review records. They remain in the archive
            and are not promoted as new developments.
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

      {headlineMetrics.length > 0 && (
        <section className="headline-grid backend-headlines">
          {headlineMetrics.map((metric) => {
            const point = metric.latest!;
            return (
              <button
                key={metric.id}
                onClick={() =>
                  openSource(observationSource(asset, metric, point))
                }
              >
                <span>{metric.label}</span>
                <strong>{formatObservationValue(point)}</strong>
                <small>
                  {periodLabel(point)} · {statusLabel(point.quality.validation)}
                </small>
              </button>
            );
          })}
        </section>
      )}

      {metrics.length > 0 ? (
        <section className="section-block backend-metric-browser">
          <div className="section-title metric-browser-title">
            <div>
              <p className="eyebrow">Source-backed history</p>
              <h2>Metric explorer</h2>
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
                  const keys = new Map<string, BackendObservation[]>();
                  for (const point of nextMetric?.points || []) {
                    const key = seriesKey(point);
                    keys.set(key, [...(keys.get(key) || []), point]);
                  }
                  setSelectedMetricId(nextId);
                  setSelectedSeriesKey(keys.keys().next().value || '');
                }}
              >
                {metrics.map((metric) => (
                  <option key={metric.id} value={metric.id}>
                    {metric.label} ({metric.presentCount}/{metric.pointCount})
                  </option>
                ))}
              </select>
            </label>
          </div>
          {selectedMetric && (
            <>
              <div className="metric-definition">
                <div>
                  <strong>{selectedMetric.label}</strong>
                  <p>
                    {selectedMetric.description ||
                      'No registry definition supplied.'}
                  </p>
                </div>
                <span>
                  {selectedMetric.presentCount} present ·{' '}
                  {selectedMetric.reviewCount} under review
                </span>
              </div>
              {groupedSeries.length > 1 && (
                <label className="series-selector">
                  <span>Filed scope series</span>
                  <select
                    value={selectedSeriesKey}
                    onChange={(event) =>
                      setSelectedSeriesKey(event.target.value)
                    }
                  >
                    {groupedSeries.map(([key, points], index) => (
                      <option key={key} value={key}>
                        Series {index + 1} · {points[0]?.period.basis} ·{' '}
                        {unitLabel(
                          points[0]?.value.display_unit ||
                            points[0]?.value.unit,
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
              {chartPoints.length > 1 ? (
                <ChartContainer
                  config={{
                    value: { label: selectedMetric.label, color: '#176b61' },
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
                          selectedPoints[0]?.value.display_unit ||
                            selectedPoints[0]?.value.unit,
                        )
                      }
                    />
                    <RechartsTooltip
                      formatter={(value) =>
                        formatBackendValue(
                          Number(value),
                          selectedPoints[0]?.value.display_unit ||
                            selectedPoints[0]?.value.unit,
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
                  This series has fewer than two usable numeric observations;
                  warned, superseded, and nonnumeric records remain below with
                  their quality and evidence.
                </div>
              )}
              <details
                className="data-table backend-data-table"
                open={chartPoints.length <= 1}
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
                        <th>Evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedPoints.map((point) => (
                        <tr key={point.id}>
                          <td>{periodLabel(point)}</td>
                          <td>{formatObservationValue(point, true)}</td>
                          <td>{statusLabel(point.quality.availability)}</td>
                          <td>
                            <span
                              className={`status status-${qualityTone(point)}`}
                            >
                              {statusLabel(point.quality.validation)}
                            </span>
                          </td>
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
