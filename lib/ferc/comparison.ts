import type { ComparisonGroupSummary } from './types';

const humanizeComparisonReason = (value: string) => {
  const spaced = value
    .trim()
    .replaceAll('_', ' ')
    .replace(/\s*:\s*/g, ': ')
    .replace(/\s+/g, ' ');
  const rendered = spaced
    ? `${spaced[0].toUpperCase()}${spaced.slice(1)}`
    : spaced;
  return rendered && !/[.!?]$/.test(rendered) ? `${rendered}.` : rendered;
};

export const MAX_COMPARISON_ASSETS = 4;

export type ComparableAsset = {
  id: string;
  name: string;
  canonicalAssetType?: string;
  canonicalAssetTypeLabel?: string;
  comparisonEligible?: boolean;
  comparisonBlockedReason?: string | null;
  comparisonGroupIds?: string[];
  comparisonGroups?: ComparisonGroupSummary[];
  comparisonSubjectIds?: string[];
};

export type ComparisonIssueCode =
  | 'duplicate'
  | 'incompatible_type'
  | 'missing_anchor_type'
  | 'missing_type'
  | 'not_found'
  | 'too_many'
  | 'duplicate_subject';

export type ComparisonIssue = {
  code: ComparisonIssueCode;
  assetId: string;
  message: string;
};

const hasRichComparisonContract = (asset: ComparableAsset) =>
  asset.comparisonGroups !== undefined;

const groupSummary = (asset: ComparableAsset, groupId: string) => {
  const matches = asset.comparisonGroups?.filter(
    (group) => group.groupId === groupId,
  );
  return matches?.length === 1 ? matches[0] : undefined;
};

const isUnambiguousGroup = (group: ComparisonGroupSummary | undefined) =>
  Boolean(
    group &&
    group.semanticKey !== 'ambiguous' &&
    group.metricCount === 1 &&
    group.seriesCount === 1 &&
    group.periodFingerprints.length > 0,
  );

export const isComparisonEligible = (
  anchor: ComparableAsset,
  candidate: ComparableAsset,
) => {
  if (
    anchor.comparisonEligible !== true ||
    candidate.comparisonEligible !== true ||
    !anchor.comparisonGroupIds?.length ||
    !candidate.comparisonGroupIds?.length ||
    !anchor.comparisonSubjectIds?.length ||
    !candidate.comparisonSubjectIds?.length
  ) {
    return false;
  }
  return commonComparisonGroupIds([anchor, candidate]).length > 0;
};

export function commonComparisonGroupIds(assets: ComparableAsset[]) {
  if (!assets.length || assets.some((asset) => !asset.comparisonGroupIds))
    return [];
  const sharedGroupIds = assets.slice(1).reduce(
    (common, asset) => {
      const groups = new Set(asset.comparisonGroupIds);
      return common.filter((group) => groups.has(group));
    },
    [...(assets[0].comparisonGroupIds ?? [])],
  );

  if (!assets.some(hasRichComparisonContract)) return sharedGroupIds;
  if (assets.some((asset) => !hasRichComparisonContract(asset))) return [];

  return sharedGroupIds.filter((groupId) => {
    const summaries = assets.map((asset) => groupSummary(asset, groupId));
    if (summaries.some((group) => !isUnambiguousGroup(group))) return false;
    const complete = summaries as ComparisonGroupSummary[];
    if (new Set(complete.map((group) => group.semanticKey ?? null)).size !== 1)
      return false;
    const commonPeriods = complete.slice(1).reduce(
      (periods, group) => {
        const ownPeriods = new Set(group.periodFingerprints);
        return periods.filter((period) => ownPeriods.has(period));
      },
      [...new Set(complete[0].periodFingerprints)],
    );
    return commonPeriods.length > 0;
  });
}

export function validateComparisonSelection(
  anchor: ComparableAsset,
  requestedIds: string[],
  assets: ComparableAsset[],
) {
  const accepted: ComparableAsset[] = [anchor];
  const issues: ComparisonIssue[] = [];
  const seen = new Set([anchor.id]);
  const seenSubjects = new Set(anchor.comparisonSubjectIds ?? []);

  if (
    anchor.comparisonEligible !== true ||
    !anchor.comparisonGroupIds?.length ||
    !anchor.comparisonSubjectIds?.length ||
    commonComparisonGroupIds([anchor]).length === 0
  ) {
    return {
      assets: accepted,
      issues: [
        {
          code: 'missing_anchor_type' as const,
          assetId: anchor.id,
          message: anchor.comparisonBlockedReason
            ? humanizeComparisonReason(anchor.comparisonBlockedReason)
            : `${anchor.name} has no eligible backend comparison series.`,
        },
      ],
    };
  }

  for (const assetId of requestedIds) {
    if (seen.has(assetId)) {
      issues.push({
        code: 'duplicate',
        assetId,
        message: `“${assetId}” appears more than once and was not added again.`,
      });
      continue;
    }
    const candidate = assets.find((asset) => asset.id === assetId);
    if (!candidate) {
      issues.push({
        code: 'not_found',
        assetId,
        message: `Asset “${assetId}” is unavailable in this snapshot.`,
      });
      continue;
    }
    const candidateSubjects = candidate.comparisonSubjectIds ?? [];
    if (candidateSubjects.some((subject) => seenSubjects.has(subject))) {
      issues.push({
        code: 'duplicate_subject',
        assetId,
        message: `${candidate.name} resolves to a filing-entity subject already selected and was not added twice.`,
      });
      continue;
    }
    if (
      candidate.comparisonEligible !== true ||
      !candidate.comparisonGroupIds?.length ||
      !candidate.comparisonSubjectIds?.length ||
      commonComparisonGroupIds([candidate]).length === 0
    ) {
      issues.push({
        code: 'missing_type',
        assetId,
        message: candidate.comparisonBlockedReason
          ? humanizeComparisonReason(candidate.comparisonBlockedReason)
          : `${candidate.name} has no eligible backend comparison series.`,
      });
      continue;
    }
    if (!isComparisonEligible(anchor, candidate)) {
      issues.push({
        code: 'incompatible_type',
        assetId,
        message: `${candidate.name} has no unambiguous comparison group with an identical reporting period in common with ${anchor.name}.`,
      });
      continue;
    }
    if (
      anchor.comparisonGroupIds &&
      commonComparisonGroupIds([...accepted, candidate]).length === 0
    ) {
      issues.push({
        code: 'incompatible_type',
        assetId,
        message: `${candidate.name} would leave the selected assets without one unambiguous backend comparison group and identical reporting period in common.`,
      });
      continue;
    }
    seen.add(assetId);
    if (accepted.length >= MAX_COMPARISON_ASSETS) {
      issues.push({
        code: 'too_many',
        assetId,
        message: `Only ${MAX_COMPARISON_ASSETS} assets can be compared at once; ${candidate.name} was not added.`,
      });
      continue;
    }
    accepted.push(candidate);
    candidateSubjects.forEach((subject) => seenSubjects.add(subject));
  }

  return { assets: accepted, issues };
}
