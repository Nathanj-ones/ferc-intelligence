import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { uniqueObservationPeriods } from '../lib/ferc/observations.ts';
import { observationSeriesKey } from '../lib/ferc/key-metrics.ts';
import {
  metricPresentation,
  observationFiledValues,
  concentrationComparisonKey,
} from '../lib/ferc/metric-presentation.ts';
import { commonComparisonGroupIds } from '../lib/ferc/comparison.ts';
import { FERC_GENERATION_ID } from '../lib/ferc/backend.ts';

const root = new URL(
  `../public/data/ferc/generations/${FERC_GENERATION_ID}/`,
  import.meta.url,
);
const directory = JSON.parse(fs.readFileSync(new URL('directory.json', root)));
const details = new Map(
  directory.assets.map((asset) => [
    asset.id,
    JSON.parse(
      gunzipSync(fs.readFileSync(new URL(`assets/${asset.id}.json.gz`, root))),
    ),
  ]),
);
let duplicates = 0;
const duplicateAssets = new Set();
for (const detail of details.values()) {
  for (const metric of detail.metrics) {
    const groups = new Map();
    for (const point of metric.points.filter(
      (point) =>
        point.quality.availability === 'present' &&
        point.quality.validation === 'pass' &&
        point.quality.version_status !== 'superseded' &&
        typeof point.value.display_value === 'number',
    )) {
      const key = observationSeriesKey(point);
      groups.set(key, [...(groups.get(key) || []), point]);
    }
    for (const points of groups.values()) {
      const result = uniqueObservationPeriods(points);
      assert.equal(result.conflictCount, 0);
      duplicates += result.duplicateCount;
      if (result.duplicateCount) duplicateAssets.add(detail.asset.id);
    }
  }
}
assert.equal(duplicates, 38);
assert.equal(duplicateAssets.size, 20);
const barrels = details
  .get('kmi-calnev')
  .metrics.find((metric) => metric.id === 'liq_barrel_miles');
const first = barrels.points.find(
  (point) => point.quality.validation === 'pass',
);
const duplicate = {
  ...structuredClone(first),
  id: 'duplicate',
  sourceRegime: 'Form 6 Page 700',
};
assert.equal(uniqueObservationPeriods([first, duplicate]).points.length, 1);
duplicate.value.display_value += 1;
assert.equal(uniqueObservationPeriods([first, duplicate]).conflictCount, 1);
duplicate.value = first.value;
duplicate.source.sourceFactId = 'different-fact';
assert.equal(uniqueObservationPeriods([first, duplicate]).conflictCount, 1);

for (const id of ['kmi-banquete-hub', 'kmi-copano-upper-gulf-coast']) {
  const metric = details
    .get(id)
    .metrics.find((metric) => metric.id === 'i311_reporting_state');
  const point = metric.points.find(
    (point) =>
      point.value.as_filed === 'activity_reported' &&
      point.source.fact?.valueAsFiled?.startsWith('{'),
  );
  assert.ok(point);
  const evidence = observationFiledValues(point);
  assert.equal(evidence.storedValue, 'activity_reported');
  assert.equal(evidence.filedValue, point.source.fact.valueAsFiled);
  assert.ok(JSON.parse(evidence.filedValue));
}
const arlington = details.get('kmi-arlington-storage');
const cadeville = details.get('wmb-cadeville-storage');
const concentration = (detail) =>
  detail.metrics.find(
    (metric) => metric.id === 'ioc_top5_shipper_concentration',
  );
assert.match(
  metricPresentation(concentration(cadeville)).label,
  /storage quantity/,
);
const mixedGroup = 'comparison-group-v1-537972230c9c1ab8973576d7';
const pointsInGroup = (detail) =>
  concentration(detail).points.filter(
    (point) =>
      point.comparison.eligible && point.comparison.group_id === mixedGroup,
  );
assert.ok(concentrationComparisonKey(pointsInGroup(cadeville)));
assert.equal(
  concentrationComparisonKey([
    ...pointsInGroup(arlington),
    ...pointsInGroup(cadeville),
  ]),
  null,
);
assert.equal(
  commonComparisonGroupIds([arlington.asset, cadeville.asset]).includes(
    mixedGroup,
  ),
  false,
);
assert.equal(
  concentrationComparisonKey([
    { ...pointsInGroup(cadeville)[0], lineage: null },
  ]),
  null,
);
assert.equal(
  details.get('oke-roadrunner-export-pipeline').asset.regime,
  'Cross-border gas pipeline',
);

// Simulate a tab with the previous projection manifest navigating after an
// update. Both stored bytes and decoded bytes must still pass verification.
const json = (value) => Buffer.from(JSON.stringify(value));
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const metadata = (bytes) => ({
  bytes: bytes.length,
  sha256: hash(bytes),
  encoding: 'identity',
  contentBytes: bytes.length,
  contentSha256: hash(bytes),
});
const testAssetId = 'test-asset';
const path = `assets/${testAssetId}.json.gz`;
const oldPayload = json({
  schema: 'ferc_site_snapshot_v1',
  generationId: FERC_GENERATION_ID,
  asset: { id: testAssetId, name: 'Old projection' },
  metrics: [],
  events: [],
});
const newPayload = json({
  schema: 'ferc_site_snapshot_v1',
  generationId: FERC_GENERATION_ID,
  asset: { id: testAssetId, name: 'Updated projection' },
  metrics: [],
  events: [],
});
const catalogPayload = json({
  schema: 'ferc_site_snapshot_v1',
  generationId: FERC_GENERATION_ID,
  assets: [
    {
      id: testAssetId,
      detailPath: `/data/ferc/generations/${FERC_GENERATION_ID}/${path}`,
    },
  ],
  instruments: [],
});
const manifest = (payload) => ({
  schema: 'ferc_site_snapshot_v1',
  generationId: FERC_GENERATION_ID,
  source: { contractVersion: '1.1.0' },
  files: {
    'directory.json': metadata(catalogPayload),
    [path]: metadata(payload),
  },
});
const realFetch = globalThis.fetch;
let updated = false;
let corrupt = false;
let manifestRequests = 0;
let assetRequests = 0;
globalThis.fetch = async (url) => {
  const href =
    typeof url === 'string' ? url : url instanceof URL ? url.href : url.url;
  if (href.endsWith('/manifest.json')) {
    manifestRequests++;
    return Response.json(manifest(updated ? newPayload : oldPayload));
  }
  if (href.includes('/directory.json')) return new Response(catalogPayload);
  if (href.includes(path)) {
    assetRequests++;
    return new Response(
      corrupt
        ? Buffer.from('corrupted bytes')
        : updated
          ? newPayload
          : oldPayload,
    );
  }
  throw new Error(`Unexpected request: ${href}`);
};
try {
  const loader = await import('../lib/ferc/backend.ts?audit-retry');
  await loader.loadFercCatalog();
  updated = true;
  await assert.rejects(
    loader.loadFercAsset(testAssetId),
    loader.FercSnapshotUpdatedError,
  );
  assert.equal(assetRequests, 2);
  assert.equal(manifestRequests, 2);
  assert.equal(
    (await loader.loadFercCatalog()).manifest.files[path].sha256,
    hash(newPayload),
  );
  assert.equal(
    (await loader.loadFercAsset(testAssetId)).asset.name,
    'Updated projection',
  );
  corrupt = true;
  const rejectingLoader =
    await import('../lib/ferc/backend.ts?audit-corruption');
  const before = assetRequests;
  await assert.rejects(
    rejectingLoader.loadFercAsset(testAssetId),
    /integrity check failed/,
  );
  assert.equal(
    assetRequests - before,
    2,
    'Corrupt data must fail after one bounded retry.',
  );
} finally {
  globalThis.fetch = realFetch;
}
console.log(
  'Deep-audit regressions passed: duplicate histories, evidence, denominator isolation, physical asset labels, and bounded integrity recovery.',
);
