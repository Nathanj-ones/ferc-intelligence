import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gunzipSync } from 'node:zlib';

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
  return (
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
const fullLineageExpected = new Map();
for (const asset of directory.assets) {
  assert.match(asset.id, /^[a-z0-9][a-z0-9-]*$/);
  assert.equal(asset.detailPath, `${publicRoot}/assets/${asset.id}.json.gz`);
  const detail = verifiedJson(`assets/${asset.id}.json.gz`);
  assert.equal(detail.asset.id, asset.id);
  assert.equal(detail.generationId, EXPECTED_GENERATION);
  assert.ok(Array.isArray(detail.asset.interests));
  assert.ok(Array.isArray(detail.asset.dockets));
  annotations += detail.annotations.length;
  if (!asset.comparisonEligible) comparisonBlocked += 1;
  assert.equal(asset.reviewCount, asset.dataSummary?.review ?? 0);
  if (asset.latestMetric) {
    assert.equal(asset.latestMetric.availability, 'present');
    assert.equal(asset.latestMetric.validation, 'pass');
    assert.notEqual(asset.latestMetric.value, null);
    assert.notEqual(asset.latestMetric.value, '');
  }
  for (const series of detail.metrics) {
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
