import assert from 'node:assert/strict';
import {
  comparisonUnitLabel,
  formatBackendValue,
  formatComparisonValue,
  formatCompact,
  formatDate,
  humanizeFercReason,
  isMetricPresentationBlocked,
  isStructuredFercValue,
  margin,
  percentChange,
  percentagePointChange,
  presentationUnit,
  selectBackendDisplayValue,
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
assert.equal(formatBackendValue(2_483_411, 'iso4217:USD', true), '$2,483,411');
assert.equal(formatComparisonValue(2_483_411, 'currency', true), '$2,483,411');
assert.equal(
  formatComparisonValue(18_374_126, 'energy', true),
  '18,374,126 Dth',
);
assert.equal(
  formatComparisonValue(1_000, 'energy_rate', true),
  '1,000 Dth/day',
);
assert.equal(
  formatComparisonValue(1.023892, 'currency_rate', true),
  '$1.023892/bbl',
);
assert.equal(formatComparisonValue(0.0954, 'fraction', true), '0.0954');
assert.equal(comparisonUnitLabel('currency'), 'US dollars (USD)');
assert.equal(
  formatComparisonValue(517, 'fraction', true, 'compressor_units'),
  '517 compressor units',
);
assert.equal(
  formatComparisonValue(117_080_088_031, 'fraction', true, 'p700_barrel_miles'),
  '117,080,088,031 barrel-miles',
);
assert.equal(
  formatComparisonValue(
    686_430_870,
    'fraction',
    true,
    'discounted_rate_volumes',
  ),
  '686,430,870 Dth',
);
assert.equal(
  presentationUnit('compressor_units', 'pure', { displayScale: 1 }),
  'compressor units',
);
assert.equal(
  presentationUnit('discounted_rate_volumes', 'pure', {
    displayScale: 1,
    origin: 'ferc_migrated',
  }),
  'Dth',
);
assert.equal(
  presentationUnit('discounted_rate_volumes', 'pure', {
    displayScale: 1,
    origin: 'native_xbrl',
  }),
  'pure',
);
assert.equal(
  presentationUnit('certificated_horsepower', 'utr:MW', { displayScale: 1 }),
  'hp',
);
assert.equal(
  presentationUnit('certificated_horsepower', 'utr:MW', {
    configuredDisplayUnit: 'MW',
    displayScale: 1,
  }),
  'utr:MW',
);
assert.equal(
  presentationUnit('certificated_horsepower', 'utr:MW', { displayScale: 100 }),
  'utr:MW',
);
assert.equal(
  isMetricPresentationBlocked('p700_barrel_miles', 'utr:bbl', 'unit_warning'),
  true,
);
assert.equal(
  isMetricPresentationBlocked('liq_barrel_miles', 'utr:bbl', 'unit_warning'),
  true,
);
assert.equal(presentationUnit('p700_wacc', 'xbrli:pure'), 'xbrli:pure');
assert.equal(formatBackendValue(9.54, 'percent', true), '9.54%');
assert.equal(
  selectBackendDisplayValue({
    as_filed: 'IS22-176: rate change filed with FERC',
    normalized_iso: '2022-01-28',
    display_value: null,
    display_unit: 'categorical',
  }),
  'IS22-176: rate change filed with FERC',
);
assert.equal(
  selectBackendDisplayValue({
    as_filed: '11/26/2025',
    normalized_iso: '2025-11-26',
    display_value: null,
    display_unit: '(date)',
  }),
  '2025-11-26',
);
assert.equal(isStructuredFercValue('{"bucket":1}'), true);
assert.equal(
  formatBackendValue('{"bucket":1}', 'categorical'),
  'Structured record · 1 field',
);
assert.equal(
  formatBackendValue('activity_reported', 'categorical'),
  'Activity reported',
);
assert.equal(
  formatBackendValue('no_reportable_activity_declared', 'categorical'),
  'No reportable activity declared',
);
assert.equal(
  humanizeFercReason('availability:source_blank'),
  'Availability: source blank.',
);

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

const comparisonGroup = (
  groupId,
  {
    metricCount = 1,
    seriesCount = 1,
    periodFingerprints = ['period-2026-q1'],
  } = {},
) => ({ groupId, metricCount, seriesCount, periodFingerprints });
const richAnchor = {
  ...backendAnchor,
  id: 'rich-anchor',
  comparisonGroupIds: ['group-volume'],
  comparisonGroups: [comparisonGroup('group-volume')],
  comparisonSubjectIds: ['rich-subject-a'],
};
const richPeer = {
  ...backendPeer,
  id: 'rich-peer',
  comparisonGroupIds: ['group-volume'],
  comparisonGroups: [comparisonGroup('group-volume')],
  comparisonSubjectIds: ['rich-subject-b'],
};
assert.equal(isComparisonEligible(richAnchor, richPeer), true);
assert.equal(
  isComparisonEligible(richAnchor, {
    ...richPeer,
    comparisonGroups: [
      comparisonGroup('group-volume', {
        metricCount: 2,
      }),
    ],
  }),
  false,
);
assert.equal(
  isComparisonEligible(richAnchor, {
    ...richPeer,
    comparisonGroups: [
      comparisonGroup('group-volume', {
        seriesCount: 2,
      }),
    ],
  }),
  false,
);
assert.equal(
  isComparisonEligible(richAnchor, {
    ...richPeer,
    comparisonGroups: [
      comparisonGroup('group-volume', {
        periodFingerprints: ['period-2025-q1'],
      }),
    ],
  }),
  false,
);
assert.equal(
  isComparisonEligible(richAnchor, {
    ...richPeer,
    comparisonGroups: undefined,
  }),
  false,
);

const firstPeriod = 'period-2026-q1';
const secondPeriod = 'period-2026-q2';
const multiPeriodAnchor = {
  ...richAnchor,
  id: 'multi-period-anchor',
  comparisonGroups: [
    comparisonGroup('group-volume', {
      periodFingerprints: [firstPeriod, secondPeriod],
    }),
  ],
  comparisonSubjectIds: ['multi-period-subject-a'],
};
const firstPeriodPeer = {
  ...richPeer,
  id: 'first-period-peer',
  comparisonGroups: [
    comparisonGroup('group-volume', {
      periodFingerprints: [firstPeriod],
    }),
  ],
  comparisonSubjectIds: ['multi-period-subject-b'],
};
const secondPeriodPeer = {
  ...richPeer,
  id: 'second-period-peer',
  comparisonGroups: [
    comparisonGroup('group-volume', {
      periodFingerprints: [secondPeriod],
    }),
  ],
  comparisonSubjectIds: ['multi-period-subject-c'],
};
assert.equal(isComparisonEligible(multiPeriodAnchor, firstPeriodPeer), true);
assert.equal(isComparisonEligible(multiPeriodAnchor, secondPeriodPeer), true);
assert.deepEqual(
  commonComparisonGroupIds([
    multiPeriodAnchor,
    firstPeriodPeer,
    secondPeriodPeer,
  ]),
  [],
);
const multiPeriodSelection = validateComparisonSelection(
  multiPeriodAnchor,
  [firstPeriodPeer.id, secondPeriodPeer.id],
  [multiPeriodAnchor, firstPeriodPeer, secondPeriodPeer],
);
assert.deepEqual(
  multiPeriodSelection.assets.map((asset) => asset.id),
  [multiPeriodAnchor.id, firstPeriodPeer.id],
);
assert.equal(multiPeriodSelection.issues[0]?.code, 'incompatible_type');

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
