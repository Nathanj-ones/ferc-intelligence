import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gunzipSync } from 'node:zlib';
import { isComparisonEligible } from '../lib/ferc/comparison.ts';
import {
  formatComparisonValue,
  isStructuredFercValue,
  supportedComparisonUnitFamilies,
} from '../lib/ferc/format.ts';
import {
  isSafeKeyMetricPoint,
  keyMetricScopeLabel,
  observationSeriesKey,
  selectAssetKeyMetrics,
} from '../lib/ferc/key-metrics.ts';

const EXPECTED_GENERATION =
  '0dccbd426f15372f1330737537f9aac58bc9547a2eaf81ef6f4b655f10cf2824';
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDirectory, '..');
const snapshotRoot = path.join(
  projectRoot,
  'public',
  'data',
  'ferc',
  'generations',
  EXPECTED_GENERATION,
);
const publicRoot = `/data/ferc/generations/${EXPECTED_GENERATION}`;
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const manifest = readJson(path.join(snapshotRoot, 'manifest.json'));
const pointer = readJson(
  path.join(projectRoot, 'public', 'data', 'ferc', 'current.json'),
);

assert.equal(manifest.schema, 'ferc_site_snapshot_v1');
assert.equal(manifest.generationId, EXPECTED_GENERATION);
assert.equal(
  manifest.source.contractSchema,
  'ferc_operating_assets_frontend_v1',
);
assert.equal(manifest.source.contractVersion, '1.1.0');
assert.equal(manifest.source.asOf, '2026-09-07');
assert.equal(pointer.generationId, EXPECTED_GENERATION);
assert.equal(pointer.root, publicRoot);

function verifiedContent(relativePath) {
  const metadata = manifest.files[relativePath];
  assert.ok(
    metadata,
    `Generated file is missing from the manifest: ${relativePath}`,
  );
  const file = path.resolve(snapshotRoot, relativePath);
  assert.ok(file.startsWith(`${snapshotRoot}${path.sep}`));
  const stored = fs.readFileSync(file);
  assert.equal(stored.length, metadata.bytes, `Size mismatch: ${relativePath}`);
  assert.equal(
    sha256(stored),
    metadata.sha256,
    `Hash mismatch: ${relativePath}`,
  );
  const content = metadata.encoding === 'gzip' ? gunzipSync(stored) : stored;
  assert.equal(
    content.length,
    metadata.contentBytes,
    `Content size mismatch: ${relativePath}`,
  );
  assert.equal(
    sha256(content),
    metadata.contentSha256,
    `Content hash mismatch: ${relativePath}`,
  );
  return content;
}

function verifiedJson(relativePath) {
  return JSON.parse(verifiedContent(relativePath));
}

for (const relativePath of Object.keys(manifest.files))
  verifiedContent(relativePath);

const directory = verifiedJson('directory.json');
const changesPayload = verifiedJson('changes.json.gz');
assert.equal(directory.assets.length, 120);
assert.equal(directory.assets.filter((asset) => asset.inScope).length, 109);
assert.equal(
  directory.assets.filter((asset) => asset.comparisonEligible).length,
  88,
);
assert.equal(new Set(directory.assets.map((asset) => asset.id)).size, 120);
assert.equal(changesPayload.changes.length, 1238);
assert.equal(changesPayload.currentCount, 0);
assert.ok(
  changesPayload.changes.every((change) => change.kind !== 'substantive'),
);
assert.ok(changesPayload.changes.every((change) => change.isBackfill));
assert.ok(changesPayload.changes.every((change) => change.firstSeen));
assert.ok(
  changesPayload.changes.every((change) => !/^9\d{3}-/.test(change.date)),
);
assert.ok(
  changesPayload.changes.every((change) =>
    change.companies.every((company) => change.company.includes(company)),
  ),
);
assert.ok(
  changesPayload.changes.every(
    (change) => new Set(change.companies).size === change.companies.length,
  ),
);
assert.ok(
  changesPayload.changes.every(
    (change) =>
      change.source.filingVersion &&
      Object.hasOwn(change.source, 'canonicalStatus'),
  ),
);
assert.ok(changesPayload.changes.some((change) => change.source.reportingDate));

const isSafeSourceUrl = (raw) => {
  if (!raw) return true;
  const value = new URL(raw);
  const placeholder =
    /^(?:<[^>]*>|redacted|placeholder|your[ _-]*(?:api[ _-]*)?key|change[ _-]*me|\*+)$/i;
  const hasPlaceholderCredential = [...value.searchParams].some(
    ([key, credential]) =>
      /(?:api[ _-]*key|access[ _-]*token|token|secret)/i.test(key) &&
      (!credential || placeholder.test(credential.trim())),
  );
  return (
    !hasPlaceholderCredential &&
    !/<redacted>|%3credacted%3e/i.test(raw) &&
    value.protocol === 'https:' &&
    (value.hostname === 'ferc.gov' ||
      value.hostname.endsWith('.ferc.gov') ||
      value.hostname === 'govinfo.gov' ||
      value.hostname === 'www.govinfo.gov')
  );
};
assert.ok(
  changesPayload.changes.every((change) => isSafeSourceUrl(change.source.url)),
);
assert.equal(manifest.inputVerification.declaredDependenciesVerified, 9);
assert.equal(manifest.inputVerification.instruments, 1);
for (const name of [
  'document_facts.csv',
  'documents.csv',
  'filing_inventory.csv',
  'lineage_edges.csv',
  'lineage_populations.csv',
  'metric_registry.csv',
  'observation_versions.csv',
  'reviewed_source_annotations.csv',
  'source_manifest.csv',
]) {
  assert.ok(manifest.files[`provenance/dependencies/${name}.gz`]);
}
assert.ok(manifest.files['provenance/source_index.json.gz']);

let annotations = 0;
let normalizedDates = 0;
let fayettevilleWarnings = 0;
let comparisonBlocked = 0;
let keyMetricCards = 0;
let assetsWithKeyMetrics = 0;
const fullLineageExpected = new Map();
const detailByAssetId = new Map();
const rawComparisonFamilies = new Set([
  'currency',
  'currency_rate',
  'percent',
  'fraction',
  'multiplier',
  'energy',
  'energy_rate',
  'volume',
  'volume_rate',
  'volume_metric',
  'liquid_volume',
  'mass_rate',
  'distance',
  'length',
  'work',
  'power',
  'count',
  'categorical',
  'date',
]);
const reviewValidations = new Set([
  'source_anomaly_review',
  'blocked_ambiguity',
  'scope_incompatible',
  'unit_warning',
  'rounding_warning',
  'source_date_warning',
]);
const pointHasValue = (point) =>
  (point.value.display_value !== null &&
    point.value.display_value !== undefined) ||
  Boolean(point.value.as_filed || point.value.normalized_iso);
const safeDirectoryValue = (point) => {
  const value =
    point.value.display_value ??
    (['date', '(date)', '(date range)'].includes(
      (point.value.display_unit || point.value.unit || '').trim().toLowerCase(),
    )
      ? point.value.normalized_iso || point.value.as_filed
      : point.value.as_filed || point.value.normalized_iso);
  return (
    (typeof value === 'number' && Number.isFinite(value)) ||
    (typeof value === 'string' &&
      value.trim().length > 0 &&
      value.length <= 160 &&
      !/[\r\n]/.test(value) &&
      !isStructuredFercValue(value))
  );
};
for (const asset of directory.assets) {
  assert.match(asset.id, /^[a-z0-9][a-z0-9-]*$/);
  assert.equal(asset.detailPath, `${publicRoot}/assets/${asset.id}.json.gz`);
  const detail = verifiedJson(`assets/${asset.id}.json.gz`);
  detailByAssetId.set(asset.id, detail);
  assert.equal(detail.asset.id, asset.id);
  assert.equal(detail.generationId, EXPECTED_GENERATION);
  assert.ok(Array.isArray(detail.asset.interests));
  assert.ok(Array.isArray(detail.asset.dockets));
  annotations += detail.annotations.length;
  if (!asset.comparisonEligible) comparisonBlocked += 1;
  assert.equal(asset.reviewCount, asset.dataSummary?.review ?? 0);
  const currentPresent = detail.metrics
    .flatMap((series) => series.points)
    .filter(
      (point) =>
        point.quality.availability === 'present' &&
        point.quality.version_status !== 'superseded',
    );
  assert.equal(
    asset.qualityFlagCount,
    currentPresent.filter((point) =>
      reviewValidations.has(point.quality.validation),
    ).length,
  );
  assert.equal(
    asset.openReviewCount,
    currentPresent.filter((point) => point.quality.review_status === 'open')
      .length,
  );
  assert.equal(
    asset.resolvedReviewCount,
    currentPresent.filter((point) => point.quality.review_status === 'resolved')
      .length,
  );
  const latestAvailablePeriod = currentPresent
    .filter(
      (point) => point.quality.validation === 'pass' && pointHasValue(point),
    )
    .map(
      (point) =>
        point.period.instant ||
        point.period.end ||
        point.dates.source_reporting_instant ||
        point.dates.source_reporting_end ||
        (/^\d{4}-\d{2}-\d{2}/.test(point.sortKey)
          ? point.sortKey.slice(0, 10)
          : null),
    )
    .filter(Boolean)
    .sort()
    .at(-1);
  assert.equal(asset.latestPeriod, latestAvailablePeriod ?? null);
  const selectedKeyMetrics = selectAssetKeyMetrics(
    detail.asset,
    detail.metrics,
  );
  assert.ok(selectedKeyMetrics.length <= 6);
  assert.equal(
    new Set(selectedKeyMetrics.map(({ metric }) => metric.id)).size,
    selectedKeyMetrics.length,
  );
  keyMetricCards += selectedKeyMetrics.length;
  if (selectedKeyMetrics.length > 0) assetsWithKeyMetrics += 1;
  for (const selection of selectedKeyMetrics) {
    assert.equal(selection.definition.metricId, selection.metric.id);
    assert.equal(
      isSafeKeyMetricPoint(selection.metric, selection.point),
      true,
    );
    assert.notEqual(
      typeof selection.point.value.as_filed === 'string' &&
        isStructuredFercValue(selection.point.value.as_filed),
      true,
    );
    for (const secondary of selection.secondary) {
      assert.equal(
        isSafeKeyMetricPoint(secondary.metric, secondary.point),
        true,
      );
      assert.equal(secondary.point.period.label, selection.point.period.label);
    }
    if (selection.definition.strategy === 'quarter') {
      assert.equal(selection.point.period.basis, 'quarter');
      if (selection.priorYearPoint) {
        assert.equal(selection.priorYearPoint.period.basis, 'quarter');
        const current = /^(\d{4})Q([1-4])$/.exec(
          selection.point.period.label,
        );
        const prior = /^(\d{4})Q([1-4])$/.exec(
          selection.priorYearPoint.period.label,
        );
        assert.ok(current && prior);
        assert.equal(Number(prior[1]), Number(current[1]) - 1);
        assert.equal(prior[2], current[2]);
      }
    }
    if (selection.definition.strategy === 'annual') {
      assert.equal(selection.point.period.basis, 'annual');
    }
    if (selection.definition.strategy === 'snapshot') {
      assert.equal(selection.point.period.basis, 'snapshot');
    }
  }
  assert.ok(Array.isArray(asset.comparisonGroups));
  for (const group of asset.comparisonGroups) {
    assert.match(group.groupId, /^comparison-group-v1-/);
    assert.ok(group.metricCount > 0);
    assert.ok(group.seriesCount > 0);
    assert.ok(group.periodFingerprints.length > 0);
    assert.ok(
      group.periodFingerprints.every((fingerprint) =>
        /^[a-f0-9]{16}$/.test(fingerprint),
      ),
    );
  }
  if (asset.latestMetric) {
    assert.equal(asset.latestMetric.availability, 'present');
    assert.equal(asset.latestMetric.validation, 'pass');
    assert.notEqual(asset.latestMetric.value, null);
    assert.notEqual(asset.latestMetric.value, '');
    assert.equal(
      typeof asset.latestMetric.value === 'string' &&
        /[\r\n]/.test(asset.latestMetric.value),
      false,
    );
    assert.ok(
      typeof asset.latestMetric.value !== 'string' ||
        asset.latestMetric.value.length <= 160,
    );
    assert.equal(
      typeof asset.latestMetric.value === 'string' &&
        (asset.latestMetric.value.trim().startsWith('{') ||
          asset.latestMetric.value.trim().startsWith('[')),
      false,
    );
  }
  const safeHeadlineExists = detail.metrics.some(
    (series) =>
      series.role === 'headline' &&
      series.latest &&
      safeDirectoryValue(series.latest),
  );
  for (const series of detail.metrics) {
    if (asset.latestMetric?.metricId === series.id) {
      assert.ok(
        series.role === 'headline' ||
          (series.id === 'i311_reporting_state' && !safeHeadlineExists),
        `Unsafe directory fallback for ${asset.id}/${series.id}`,
      );
    }
    for (const point of series.points) {
      assert.ok(point.quality);
      assert.ok(point.scope);
      assert.ok(point.dates);
      assert.ok(point.value);
      assert.ok(isSafeSourceUrl(point.source.url));
      assert.ok(Object.hasOwn(point.source, 'canonical'));
      if (point.value.normalized_iso) normalizedDates += 1;
      if (
        asset.id === 'kmi-fayetteville-express' &&
        point.value.as_filed === '999999' &&
        point.quality.review_status === 'open' &&
        point.quality.qa_flags?.includes('preserved exactly as filed')
      ) {
        fayettevilleWarnings += 1;
      }
      if (point.comparison?.eligible) {
        assert.ok(asset.comparisonEligible);
        assert.ok(asset.comparisonGroupIds.includes(point.comparison.group_id));
        assert.ok(
          asset.comparisonSubjectIds.includes(point.comparison.subject_id),
        );
        assert.notEqual(point.comparison.comparison_value_base, null);
        assert.ok(point.comparison.base_unit_family);
        assert.ok(
          supportedComparisonUnitFamilies.has(
            point.comparison.base_unit_family,
          ),
        );
        const renderedComparison = formatComparisonValue(
          point.comparison.comparison_value_base,
          point.comparison.base_unit_family,
          true,
          series.id,
        );
        assert.equal(/\b(?:undefined|nan)\b/i.test(renderedComparison), false);
        assert.equal(
          rawComparisonFamilies.has(
            renderedComparison.split(/\s+/).at(-1)?.toLowerCase(),
          ),
          false,
          `Raw comparison family leaked for ${asset.id}/${series.id}`,
        );
        if (series.id === 'certificated_horsepower') {
          assert.equal(
            asset.comparisonGroups.some(
              (group) => group.groupId === point.comparison.group_id,
            ),
            false,
          );
        }
      }
      if (
        point.lineage &&
        (!point.lineage.edgeSampleComplete ||
          !point.lineage.populationSampleComplete)
      ) {
        assert.equal(
          point.lineage.fullPath,
          `${publicRoot}/provenance/lineage-${point.id.slice(4, 6)}.json.gz`,
        );
        fullLineageExpected.set(point.id, {
          path: `provenance/lineage-${point.id.slice(4, 6)}.json.gz`,
          edges: point.lineage.edgeCount,
          populations: point.lineage.populationCount,
        });
      }
    }
  }
}

const selectedFor = (assetId) => {
  const detail = detailByAssetId.get(assetId);
  assert.ok(detail, `Missing key-metric fixture: ${assetId}`);
  return selectAssetKeyMetrics(detail.asset, detail.metrics);
};
const hilandKeyMetrics = selectedFor('kmi-hiland-express');
assert.deepEqual(
  hilandKeyMetrics.map(({ metric }) => metric.id),
  [
    'liq_operating_revenue',
    'liq_net_carrier_operating_income',
    'liq_barrels_delivered',
    'p700_interstate_operating_revenue',
  ],
);
assert.equal(hilandKeyMetrics[0].point.period.label, '2026Q2');
assert.equal(hilandKeyMetrics[0].point.value.display_value, 34442116);
assert.equal(hilandKeyMetrics[0].priorYearPoint?.period.label, '2025Q2');
assert.equal(
  hilandKeyMetrics[1].secondary[0]?.metric.id,
  'liq_operating_margin_pct',
);
assert.deepEqual(
  hilandKeyMetrics[3].secondary.map(({ metric }) => metric.id),
  [
    'p700_total_cost_of_service',
    'p700_revenue_to_cost_ratio',
    'p700_revenue_less_cost_of_service',
  ],
);
assert.deepEqual(
  selectedFor('wmb-transco')
    .slice(0, 4)
    .map(({ metric }) => metric.id),
  [
    'gas_operating_revenues',
    'net_utility_operating_income',
    'total_throughput',
    'ioc_firm_transport_mdq',
  ],
);
const storageConcentration = selectedFor('wmb-cadeville-storage').find(
  ({ metric }) => metric.id === 'ioc_top5_shipper_concentration',
);
assert.equal(
  storageConcentration?.definition.label,
  'Top-five shipper share of contracted storage quantity',
);
for (const assetId of ['kmi-banquete-hub', 'kmi-copano-upper-gulf-coast']) {
  const selected = selectedFor(assetId);
  assert.deepEqual(
    selected.map(({ metric }) => metric.id),
    ['i311_reporting_state'],
  );
  assert.equal(selected[0].point.value.as_filed, 'activity_reported');
  assert.equal(selected[0].priorYearPoint, null);
}
assert.equal(
  selectedFor('kmi-net-mexico-pipeline').some(
    ({ metric }) => metric.id === 'i311_billed_transport_usage',
  ),
  false,
);
assert.deepEqual(selectedFor('wmb-altura-cogeneration-facility'), []);
assert.deepEqual(
  selectedFor('lng-corpus-christi-liquefaction-trains-1-3'),
  [],
);
const elbaAuthorisation = selectedFor('kmi-elba-liquefaction').find(
  ({ metric }) => metric.id === 'lng_status_authorised',
);
assert.equal(
  keyMetricScopeLabel(elbaAuthorisation.point),
  'Elba Island LNG Terminal (Chatham County, GA) · Movable Modular Liquefaction System 2',
);
const arbuckleNorthKeyMetric = selectedFor('oke-arbuckle-north')[0];
assert.equal(arbuckleNorthKeyMetric.point.value.display_value, 51013551);
assert.ok(
  arbuckleNorthKeyMetric.metric.points
    .filter(
      (point) =>
        observationSeriesKey(point) ===
        observationSeriesKey(arbuckleNorthKeyMetric.point),
    )
    .some((point) => point.id === arbuckleNorthKeyMetric.point.id),
);
assert.equal(assetsWithKeyMetrics, 102);
assert.equal(keyMetricCards, 386);

const assetById = new Map(directory.assets.map((asset) => [asset.id, asset]));
const comparisonPair = (leftId, rightId) => {
  const left = assetById.get(leftId);
  const right = assetById.get(rightId);
  assert.ok(left && right, `Missing comparison fixture: ${leftId}/${rightId}`);
  return isComparisonEligible(left, right);
};
assert.equal(comparisonPair('wmb-transco', 'kmi-tennessee-gas-pipeline'), true);
for (const [left, right] of [
  ['kmi-gulf-lng-pipeline', 'wmb-gulfstream'],
  ['kmi-gulf-lng-pipeline', 'wmb-mountainwest-overthrust'],
  ['kmi-gulf-lng-pipeline', 'kmi-tennessee-gas-pipeline'],
  ['oke-bridgeline', 'kmi-keystone-gas-storage'],
  ['oke-bridgeline', 'oke-louisiana-intrastate-gas'],
  ['kmi-gulf-lng-terminal', 'lng-sabine-pass-lng-terminal'],
  ['oke-louisiana-intrastate-gas', 'oke-matterhorn-express'],
]) {
  assert.equal(comparisonPair(left, right), false, `${left}/${right}`);
}
assert.equal(
  directory.assets.find((asset) => asset.id === 'trgp-badlands-crude')
    ?.latestMetric?.value,
  'IS26-24: accepted AND SUSPENDED, subject to refund -- refund clock started',
);
for (const [assetId, usable] of [
  ['kmi-banquete-hub', 44],
  ['kmi-copano-upper-gulf-coast', 54],
]) {
  const asset = directory.assets.find((item) => item.id === assetId);
  assert.ok(asset);
  assert.equal(asset.latestPeriod, '2026-06-30');
  assert.equal(asset.dataSummary.usable, usable);
  assert.equal(asset.latestMetric?.metricId, 'i311_reporting_state');
  assert.equal(asset.latestMetric?.value, 'activity_reported');
}
assert.equal(
  directory.assets.find((asset) => asset.id === 'wmb-nortex-worsham-steed')
    ?.latestPeriod,
  '2026-03-31',
);

assert.equal(
  directory.assets.find((asset) => asset.id === 'oke-northern-border')?.company,
  'TC Energy',
);
assert.equal(
  directory.assets.find((asset) => asset.id === 'oke-overland-pass')?.company,
  'Williams',
);
assert.ok(
  changesPayload.changes
    .filter((change) => change.assetIds.includes('oke-northern-border'))
    .every(
      (change) =>
        change.companies.includes('TC Energy') &&
        !change.companies.includes('ONEOK'),
    ),
);

assert.equal(directory.instruments.length, 1);
const instrument = directory.instruments[0];
assert.equal(instrument.id, 'ferc-oil-index');
assert.equal(
  instrument.detailPath,
  `${publicRoot}/instruments/ferc-oil-index.json.gz`,
);
const instrumentDetail = verifiedJson('instruments/ferc-oil-index.json.gz');
assert.equal(instrumentDetail.instrument.entityKey, 'FERC-OIL-INDEX');
assert.equal(instrumentDetail.summary.observations, 60);
assert.equal(instrumentDetail.summary.review, 8);
const instrumentCurrentPresent = instrumentDetail.metrics
  .flatMap((metric) => metric.points)
  .filter(
    (point) =>
      point.quality.availability === 'present' &&
      point.quality.version_status !== 'superseded',
  );
assert.equal(
  instrument.qualityFlagCount,
  instrumentCurrentPresent.filter((point) =>
    reviewValidations.has(point.quality.validation),
  ).length,
);
assert.equal(
  instrument.openReviewCount,
  instrumentCurrentPresent.filter(
    (point) => point.quality.review_status === 'open',
  ).length,
);
assert.equal(
  instrument.resolvedReviewCount,
  instrumentCurrentPresent.filter(
    (point) => point.quality.review_status === 'resolved',
  ).length,
);
assert.equal(
  instrumentDetail.metrics.flatMap((metric) => metric.points).length,
  60,
);
assert.equal(
  instrumentDetail.metrics
    .flatMap((metric) => metric.points)
    .filter((point) => point.comparison.eligible).length,
  52,
);
assert.ok(
  instrumentDetail.metrics
    .flatMap((metric) => metric.points)
    .every((point) => isSafeSourceUrl(point.source.url)),
);
assert.ok(
  instrumentDetail.metrics
    .flatMap((metric) => metric.points)
    .every((point) => {
      if (!point.source.url) return false;
      const hostname = new URL(point.source.url).hostname;
      return hostname === 'govinfo.gov' || hostname === 'www.govinfo.gov';
    }),
);

assert.equal(annotations, 7);
assert.equal(fayettevilleWarnings, 4);
assert.ok(
  normalizedDates > 0,
  'Normalized ISO values must survive separately from period dates.',
);
assert.equal(comparisonBlocked, 32);
assert.equal(
  fullLineageExpected.size,
  manifest.inputVerification.incompleteLineageObservationsSharded,
);
const lineageShardCache = new Map();
for (const [observationId, expected] of fullLineageExpected) {
  let shard = lineageShardCache.get(expected.path);
  if (!shard) {
    shard = verifiedJson(expected.path);
    lineageShardCache.set(expected.path, shard);
  }
  const lineage = shard.observations[observationId];
  assert.ok(lineage, `Missing complete lineage: ${observationId}`);
  assert.equal(lineage.edges.length, expected.edges);
  assert.equal(lineage.populations.length, expected.populations);
}

const sharedAliases = directory.assets.filter(
  (asset) => asset.scopeRelation === 'shared_filer_entity_context',
);
assert.equal(sharedAliases.length, 10);
assert.ok(sharedAliases.every((asset) => !asset.comparisonEligible));
assert.ok(
  directory.assets
    .filter((asset) => !asset.inScope)
    .every((asset) => !asset.comparisonEligible),
);

const deployedFiles = fs
  .readdirSync(path.join(snapshotRoot, 'assets'))
  .filter((name) => name.endsWith('.json.gz'))
  .sort();
assert.deepEqual(
  deployedFiles,
  directory.assets.map((asset) => `${asset.id}.json.gz`).sort(),
  'Generated asset route closure differs from the directory whitelist.',
);
const deployedInstruments = fs
  .readdirSync(path.join(snapshotRoot, 'instruments'))
  .filter((name) => name.endsWith('.json.gz'))
  .sort();
assert.deepEqual(
  deployedInstruments,
  directory.instruments.map((item) => `${item.id}.json.gz`).sort(),
  'Generated instrument route closure differs from the directory whitelist.',
);

console.log(
  `Backend snapshot checks passed: ${directory.assets.length} assets, ${changesPayload.changes.length} historical events, ${annotations} annotations.`,
);
