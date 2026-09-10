import assert from 'node:assert/strict';
import {
  formatCompact,
  formatDate,
  margin,
  percentChange,
  percentagePointChange,
} from '../lib/ferc/format.ts';
import {
  changes,
  operatingAssets,
  projectSource,
  projects,
} from '../lib/ferc/data.ts';
import {
  MAX_COMPARISON_ASSETS,
  commonComparisonGroupIds,
  isComparisonEligible,
  validateComparisonSelection,
} from '../lib/ferc/comparison.ts';

assert.equal(formatCompact(null), 'Unavailable');
assert.equal(formatCompact(1_250_000_000, 'volume'), '1.25bn');
assert.equal(margin(25, 100), 25);
assert.equal(margin(25, 0), null);
assert.equal(percentChange(10, null), null);
assert.equal(percentChange(10, 0), null);
assert.equal(percentChange(10, -1), null);
assert.equal(percentChange(110, 100), 10);
assert.equal(percentagePointChange(42, 39.5), 2.5);
assert.equal(formatDate('2026-08-19T00:00:00'), '19 Aug 2026');
assert.equal(formatDate('not-a-date'), 'Unavailable');

assert.ok(changes.every((item) => item.firstSeen === undefined));
assert.equal(
  projects.find((project) => project.docket === 'CP25-219')?.regulatory_outlook,
  null,
);
assert.ok(
  operatingAssets.every((asset) =>
    asset.quarters.every((point) =>
      asset.sources.some((source) => source.id === point.sourceId),
    ),
  ),
);

const comparisonFixtures = operatingAssets.map((asset) => ({
  ...asset,
  comparisonEligible: true,
  comparisonGroupIds: ['fixture-group'],
  comparisonSubjectIds: [`fixture-subject-${asset.id}`],
}));
const transco = comparisonFixtures.find((asset) => asset.id === 'transco');
const tgp = comparisonFixtures.find((asset) => asset.id === 'tgp');
assert.ok(transco && tgp);
assert.equal(MAX_COMPARISON_ASSETS, 4);
assert.equal(isComparisonEligible(transco, tgp), true);
assert.equal(
  validateComparisonSelection(
    transco,
    ['tgp', 'elba-express', 'transcolorado'],
    comparisonFixtures,
  ).assets.length,
  4,
);

const duplicate = validateComparisonSelection(
  transco,
  ['tgp', 'tgp'],
  comparisonFixtures,
);
assert.equal(duplicate.assets.length, 2);
assert.equal(duplicate.issues[0]?.code, 'duplicate');

const fifth = validateComparisonSelection(
  transco,
  ['tgp', 'elba-express', 'transcolorado', 'fifth-test-only'],
  [
    ...comparisonFixtures,
    {
      ...tgp,
      id: 'fifth-test-only',
      name: 'Test-only fifth asset',
      comparisonSubjectIds: ['fixture-subject-fifth'],
    },
  ],
);
assert.equal(fifth.assets.length, 4);
assert.equal(fifth.issues[0]?.code, 'too_many');

const duplicateAfterLimit = validateComparisonSelection(
  transco,
  ['tgp', 'elba-express', 'transcolorado', 'tgp'],
  comparisonFixtures,
);
assert.equal(duplicateAfterLimit.assets.length, 4);
assert.equal(duplicateAfterLimit.issues[0]?.code, 'duplicate');

const incompatible = validateComparisonSelection(
  transco,
  ['test-only-storage'],
  [
    ...comparisonFixtures,
    {
      ...tgp,
      id: 'test-only-storage',
      name: 'Test-only storage asset',
      canonicalAssetType: 'dedicated_gas_storage',
      canonicalAssetTypeLabel: 'Dedicated gas storage',
      comparisonGroupIds: ['fixture-storage-group'],
    },
  ],
);
assert.equal(incompatible.assets.length, 1);
assert.equal(incompatible.issues[0]?.code, 'incompatible_type');
assert.equal(
  validateComparisonSelection(transco, ['missing'], comparisonFixtures)
    .issues[0]?.code,
  'not_found',
);

const backendAnchor = {
  ...transco,
  id: 'backend-anchor',
  comparisonEligible: true,
  comparisonGroupIds: ['group-revenue', 'group-volume'],
  comparisonSubjectIds: ['subject-a'],
};
const backendPeer = {
  ...tgp,
  id: 'backend-peer',
  comparisonEligible: true,
  comparisonGroupIds: ['group-volume', 'group-balance'],
  comparisonSubjectIds: ['subject-b'],
};
const backendWrongGroup = {
  ...tgp,
  id: 'backend-wrong-group',
  comparisonEligible: true,
  comparisonGroupIds: ['group-unrelated'],
  comparisonSubjectIds: ['subject-c'],
};
const backendDuplicateSubject = {
  ...tgp,
  id: 'backend-duplicate-subject',
  comparisonEligible: true,
  comparisonGroupIds: ['group-volume'],
  comparisonSubjectIds: ['subject-a'],
};
assert.deepEqual(commonComparisonGroupIds([backendAnchor, backendPeer]), [
  'group-volume',
]);
assert.equal(isComparisonEligible(backendAnchor, backendPeer), true);
assert.equal(isComparisonEligible(backendAnchor, backendWrongGroup), false);
assert.equal(
  isComparisonEligible(backendAnchor, {
    ...backendPeer,
    comparisonGroupIds: undefined,
  }),
  false,
);
assert.equal(
  isComparisonEligible(backendAnchor, {
    ...backendPeer,
    comparisonSubjectIds: undefined,
  }),
  false,
);
assert.equal(
  validateComparisonSelection(
    backendAnchor,
    [backendPeer.id, backendWrongGroup.id],
    [backendAnchor, backendPeer, backendWrongGroup],
  ).issues[0]?.code,
  'incompatible_type',
);
assert.equal(
  validateComparisonSelection(
    backendAnchor,
    [backendDuplicateSubject.id],
    [backendAnchor, backendDuplicateSubject],
  ).issues[0]?.code,
  'duplicate_subject',
);

const project = projects.find((item) => item.docket === 'CP25-219');
assert.ok(project);
const representativeSource = projectSource(
  project,
  '20260901-5206',
  'Construction / Compliance',
  undefined,
  'Supplied activity summary',
);
assert.equal(representativeSource.period, undefined);
assert.equal(representativeSource.description, 'Supplied activity summary');

console.log('Frontend formatting and comparison checks passed.');
