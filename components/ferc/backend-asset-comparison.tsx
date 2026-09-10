'use client';

import { useMemo, useState } from 'react';
import { ArrowLeft, CircleAlert, Info, Plus, RefreshCw, X } from 'lucide-react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { ChartContainer } from '@/components/ui/chart';
import {
  commonComparisonGroupIds,
  MAX_COMPARISON_ASSETS,
} from '@/lib/ferc/comparison';
import {
  comparisonUnitLabel,
  formatComparisonValue,
  humanizeFercText,
  isMetricPresentationBlocked,
  sourceUnitLabel,
} from '@/lib/ferc/format';
import type {
  BackendMetricSeries,
  BackendObservation,
  OperatingAssetDetail,
  SourceDetail,
} from '@/lib/ferc/types';
import { observationSource } from './backend-asset-detail';

const colors = ['#176b61', '#4f70a3', '#885d83', '#73815b'];
const dashes = [undefined, '8 4', '3 3', '10 3 2 3'];

const periodKey = (point: BackendObservation) =>
  [
    point.period.basis,
    point.period.start,
    point.period.end,
    point.period.instant,
    point.period.label,
  ].join('|');

const periodLabel = (point: BackendObservation) =>
  point.period.label ||
  point.period.instant ||
  point.period.end ||
  point.sortKey;

type ComparableSeries = {
  detail: OperatingAssetDetail;
  metric: BackendMetricSeries;
  points: BackendObservation[];
};

type ComparableGroup = {
  id: string;
  metricId: string;
  label: string;
  description: string | null;
  unitFamily: string;
  scopeContract: string;
  periodBasis: string;
  scopeLabel: string;
  sourceLabel: string;
  series: ComparableSeries[];
  commonPeriodKeys: string[];
};

const scopeDiscriminator = (scope: string) => {
  const bucket = /remaining primary term bucket ([^|;]+)/i.exec(scope)?.[1];
  if (bucket) return `Bucket: ${humanizeFercText(bucket)}`;
  if (scope.includes('|')) return humanizeFercText(scope);
  const segments = scope
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean);
  const detail =
    segments.find((part) =>
      /context dimensions|aggregate|consolidated source context/i.test(part),
    ) ||
    segments.at(-1) ||
    scope;
  const rendered = humanizeFercText(detail);
  return rendered.length > 100
    ? `${rendered.slice(0, 48)}…${rendered.slice(-48)}`
    : rendered;
};

const comparisonOptionLabel = (
  group: ComparableGroup,
  groups: ComparableGroup[],
) => {
  const base = `${group.label} · ${humanizeFercText(group.periodBasis)} · ${comparisonUnitLabel(group.unitFamily, group.metricId)} · ${group.scopeLabel}`;
  const withSource = `${base} · ${group.sourceLabel}`;
  const baseMatches = groups.filter(
    (candidate) =>
      `${candidate.label} · ${humanizeFercText(candidate.periodBasis)} · ${comparisonUnitLabel(candidate.unitFamily, candidate.metricId)} · ${candidate.scopeLabel}` ===
      base,
  );
  if (baseMatches.length === 1) return base;
  const sourceMatches = baseMatches.filter(
    (candidate) => `${base} · ${candidate.sourceLabel}` === withSource,
  );
  return sourceMatches.length === 1
    ? withSource
    : `${withSource} · Contract ref ${group.id.slice(-6)}`;
};

function buildGroups(details: OperatingAssetDetail[]): ComparableGroup[] {
  return commonComparisonGroupIds(
    details.map((detail) => detail.asset),
  ).flatMap((groupId) => {
    const series = details.map((detail) => {
      const matches = detail.metrics.flatMap((metric) =>
        metric.points
          .filter(
            (point) =>
              metric.id !== 'certificated_horsepower' &&
              !isMetricPresentationBlocked(
                metric.id,
                point.value.display_unit || point.value.unit,
                point.quality.validation,
              ) &&
              point.comparison.eligible &&
              point.comparison.group_id === groupId &&
              typeof point.comparison.comparison_value_base === 'number',
          )
          .map((point) => ({ metric, point })),
      );
      const seriesIds = new Set(
        matches.map(({ point }) => point.comparison.series_id),
      );
      const metricIds = new Set(matches.map(({ metric }) => metric.id));
      if (!matches.length || seriesIds.size !== 1 || metricIds.size !== 1)
        return null;
      return {
        detail,
        metric: matches[0].metric,
        points: matches
          .map(({ point }) => point)
          .sort((left, right) => left.sortKey.localeCompare(right.sortKey)),
      };
    });
    if (series.some((entry) => entry === null)) return [];
    const complete = series as ComparableSeries[];
    const first = complete[0].points[0];
    const unitFamily = first.comparison.base_unit_family;
    const scopeContract = first.comparison.scope_contract;
    if (!unitFamily || !scopeContract) return [];
    const identitiesMatch = complete.every((entry) =>
      entry.points.every(
        (point) =>
          point.comparison.base_unit_family === unitFamily &&
          point.comparison.scope_contract === scopeContract,
      ),
    );
    const periodsAreUnique = complete.every(
      (entry) =>
        new Set(entry.points.map(periodKey)).size === entry.points.length,
    );
    if (!identitiesMatch || !periodsAreUnique) return [];
    const commonPeriodKeys = complete.slice(1).reduce((common, entry) => {
      const own = new Set(entry.points.map(periodKey));
      return common.filter((key) => own.has(key));
    }, complete[0].points.map(periodKey));
    if (commonPeriodKeys.length === 0) return [];
    return [
      {
        id: groupId,
        metricId: complete[0].metric.id,
        label: complete[0].metric.label,
        description: complete[0].metric.description,
        unitFamily,
        scopeContract,
        periodBasis: first.period.basis,
        scopeLabel: scopeDiscriminator(first.scope.actual),
        sourceLabel: `${humanizeFercText(first.quality.origin)} · ${sourceUnitLabel(first.value.display_unit || first.value.unit) || 'unit not supplied'}`,
        series: complete,
        commonPeriodKeys,
      },
    ];
  });
}

export function BackendAssetComparison({
  details,
  issues,
  back,
  add,
  replace,
  remove,
  exit,
  openSource,
}: {
  details: OperatingAssetDetail[];
  issues: string[];
  back: () => void;
  add: () => void;
  replace: (id: string) => void;
  remove: (id: string) => void;
  exit: () => void;
  openSource: (source: SourceDetail) => void;
}) {
  const groups = useMemo(() => buildGroups(details), [details]);
  const [selectedGroupId, setSelectedGroupId] = useState(groups[0]?.id || '');
  const selectedGroup =
    groups.find((group) => group.id === selectedGroupId) || groups[0];
  const assets = details.map((detail) => detail.asset);
  const anchor = assets[0];
  const latestCommonKey = selectedGroup?.commonPeriodKeys.at(-1);
  const latestCommonPoints = selectedGroup?.series.map((entry) =>
    entry.points.find((point) => periodKey(point) === latestCommonKey),
  );
  const chartData = (selectedGroup?.commonPeriodKeys || []).map((key) => {
    const row: Record<string, string | number> = { period: key };
    selectedGroup!.series.forEach((entry, index) => {
      const point = entry.points.find(
        (candidate) => periodKey(candidate) === key,
      )!;
      row.label = periodLabel(point);
      row[`asset${index}`] = point.comparison.comparison_value_base as number;
    });
    return row;
  });

  return (
    <main className="page-shell detail-page comparison-page backend-comparison-page">
      <button className="back-button" onClick={back}>
        <ArrowLeft /> Back to asset
      </button>
      <header className="comparison-titlebar">
        <div>
          <div className="detail-badges">
            <span className="status status-neutral">
              {anchor.canonicalAssetTypeLabel}
            </span>
            <span className="status status-project">
              Receipt-gated comparison
            </span>
          </div>
          <h1 id="asset-comparison-title">Asset comparison</h1>
          <p>
            {assets.length} of {MAX_COMPARISON_ASSETS} distinct filing subjects
            · exact backend groups and base units only
          </p>
        </div>
        <div className="comparison-actions">
          {assets.length < MAX_COMPARISON_ASSETS && (
            <button className="compare-button" onClick={add}>
              <Plus /> Add asset
            </button>
          )}
          <button className="secondary-button" onClick={exit}>
            Exit comparison
          </button>
        </div>
      </header>

      {issues.length > 0 && (
        <output className="comparison-warning">
          <CircleAlert />
          <div>
            <strong>Some requested selections were excluded</strong>
            {issues.map((issue) => (
              <p key={issue}>{issue}</p>
            ))}
          </div>
        </output>
      )}

      {selectedGroup ? (
        <>
          <section className="comparison-basis backend-comparison-basis">
            <div>
              <label>
                <span>Exact comparison group</span>
                <select
                  value={selectedGroup.id}
                  onChange={(event) => setSelectedGroupId(event.target.value)}
                >
                  {groups.map((group) => (
                    <option key={group.id} value={group.id}>
                      {comparisonOptionLabel(group, groups)}
                    </option>
                  ))}
                </select>
              </label>
              <strong>
                {latestCommonPoints?.[0]
                  ? `Latest common period · ${periodLabel(latestCommonPoints[0])}`
                  : 'No identical reporting period is available'}
              </strong>
            </div>
            <p>
              {selectedGroup.description ||
                'No metric-registry definition supplied.'}
            </p>
          </section>
          <div className="metric-scope-note">
            <Info />
            <p>
              <strong>Comparison contract:</strong>{' '}
              {humanizeFercText(selectedGroup.scopeContract)} · base unit{' '}
              {comparisonUnitLabel(
                selectedGroup.unitFamily,
                selectedGroup.metricId,
              )}
              . Values are the backend’s base comparison values, not frontend
              conversions.
            </p>
          </div>

          <p className="comparison-scroll-hint">
            Scroll sideways to inspect every asset column.
          </p>
          <section
            className="comparison-scroll"
            aria-labelledby="asset-comparison-title"
          >
            <div
              className="comparison-matrix"
              style={{ '--asset-count': assets.length } as React.CSSProperties}
            >
              <div className="comparison-corner">Latest aligned value</div>
              {assets.map((asset, index) => (
                <div className="comparison-asset-head" key={asset.id}>
                  <i style={{ background: colors[index] }} />
                  <small>{asset.company}</small>
                  <strong>{asset.name}</strong>
                  <span>{asset.legalFiler}</span>
                  <em>{selectedGroup.series[index].points[0].scope.actual}</em>
                  {index === 0 ? (
                    <b>Original asset</b>
                  ) : (
                    <div>
                      <button
                        aria-label={`Replace ${asset.name}`}
                        onClick={() => replace(asset.id)}
                      >
                        <RefreshCw /> Replace
                      </button>
                      <button
                        aria-label={`Remove ${asset.name}`}
                        onClick={() => remove(asset.id)}
                      >
                        <X /> Remove
                      </button>
                    </div>
                  )}
                </div>
              ))}
              <div className="comparison-row-label">
                <strong>{selectedGroup.label}</strong>
                <span>
                  {comparisonUnitLabel(
                    selectedGroup.unitFamily,
                    selectedGroup.metricId,
                  )}{' '}
                  · identical period required
                </span>
              </div>
              {assets.map((asset, index) => {
                const point = latestCommonPoints?.[index];
                const entry = selectedGroup.series[index];
                return (
                  <button
                    key={asset.id}
                    className="comparison-value"
                    disabled={!point}
                    onClick={() =>
                      point &&
                      openSource(observationSource(asset, entry.metric, point))
                    }
                  >
                    <strong>
                      {formatComparisonValue(
                        point?.comparison.comparison_value_base,
                        selectedGroup.unitFamily,
                        true,
                        selectedGroup.metricId,
                      )}
                    </strong>
                    <span>
                      {point ? periodLabel(point) : 'No common period'}
                    </span>
                    {point && <em>Evidence and quality</em>}
                  </button>
                );
              })}
            </div>
          </section>

          <section className="chart-card backend-comparison-chart-card">
            <div className="chart-heading">
              <div>
                <p className="eyebrow">Aligned history</p>
                <h2>{selectedGroup.label}</h2>
                <p>
                  Only identical reporting periods in the exact backend group
                  are plotted.
                </p>
              </div>
            </div>
            {chartData.length ? (
              <ChartContainer config={{}} className="comparison-chart">
                <LineChart
                  data={chartData}
                  margin={{ left: 8, right: 20, top: 20, bottom: 4 }}
                >
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="label" />
                  <YAxis
                    width={72}
                    tickFormatter={(value) =>
                      formatComparisonValue(
                        Number(value),
                        selectedGroup.unitFamily,
                        false,
                        selectedGroup.metricId,
                      )
                    }
                  />
                  <RechartsTooltip
                    formatter={(value) =>
                      formatComparisonValue(
                        Number(value),
                        selectedGroup.unitFamily,
                        true,
                        selectedGroup.metricId,
                      )
                    }
                  />
                  <Legend />
                  {assets.map((asset, index) => (
                    <Line
                      key={asset.id}
                      type="linear"
                      dataKey={`asset${index}`}
                      name={asset.name}
                      stroke={colors[index]}
                      strokeDasharray={dashes[index]}
                      strokeWidth={2.5}
                      connectNulls={false}
                    />
                  ))}
                </LineChart>
              </ChartContainer>
            ) : (
              <div className="chart-empty">
                These eligible series do not share an identical reporting
                period, so no values are aligned or ranked.
              </div>
            )}
            <details className="data-table backend-data-table">
              <summary>View aligned records</summary>
              <div className="data-table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Period</th>
                      {assets.map((asset) => (
                        <th key={asset.id}>{asset.name}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {chartData.map((row) => (
                      <tr key={row.period}>
                        <td>{row.label}</td>
                        {assets.map((asset, index) => {
                          const point = selectedGroup.series[index].points.find(
                            (candidate) => periodKey(candidate) === row.period,
                          );
                          return (
                            <td key={asset.id}>
                              {point ? (
                                <button
                                  className="comparison-table-source"
                                  onClick={() =>
                                    openSource(
                                      observationSource(
                                        asset,
                                        selectedGroup.series[index].metric,
                                        point,
                                      ),
                                    )
                                  }
                                >
                                  <span>
                                    {formatComparisonValue(
                                      point.comparison.comparison_value_base,
                                      selectedGroup.unitFamily,
                                      true,
                                      selectedGroup.metricId,
                                    )}
                                  </span>
                                  <small>Details</small>
                                </button>
                              ) : (
                                '—'
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
        </>
      ) : (
        <section className="section-block identity-only-state">
          <CircleAlert />
          <div>
            <p className="eyebrow">Comparison unavailable</p>
            <h2>No unambiguous shared series</h2>
            <p>
              The selected assets passed directory eligibility, but their detail
              payloads do not expose one unique comparable series with a shared
              exact contract. No substitute was inferred.
            </p>
          </div>
        </section>
      )}
    </main>
  );
}
