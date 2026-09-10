export type DataStatus = {
  availability:
    | 'present'
    | 'not_retrieved'
    | 'parse_failed'
    | 'source_blank'
    | 'unknown';
  origin:
    | 'ferc_migrated'
    | 'native_xbrl'
    | 'elibrary_document'
    | 'backend_assessment';
  method: 'filed' | 'derived' | 'backend_assessment';
  version: 'original' | 'revised' | 'unresolved';
  validation: 'pass' | 'warning' | 'not_assessed';
};

export type SourceDetail = {
  id: string;
  title: string;
  sourceSystem: string;
  nativeIdentity?: string;
  accession?: string;
  docket?: string;
  filingVersion?: string;
  canonicalStatus?: string;
  canonicalReason?: string;
  acceptanceStatus?: string;
  dataOrigin?: string;
  period?: string;
  scope?: string;
  unit?: string;
  displayUnit?: string;
  filedUnit?: string;
  displayScale?: number | null;
  value?: string;
  valueLabel?: string;
  filedValue?: string;
  filedValueRaw?: string;
  method: string;
  availability: string;
  origin: string;
  validation: string;
  description?: string;
  formula?: string;
  inputs?: {
    label: string;
    value: string;
    period?: string;
    sourceId?: string;
    details?: string[];
  }[];
  populations?: {
    id: string;
    source?: string;
    filingIds?: string[];
    inclusionRule?: string;
    exclusionRule?: string;
    rowCount?: number;
    candidateCount?: number;
    excludedCount?: number;
    memberDigest?: string;
    aggregate?: string;
    note?: string;
  }[];
  comparisonValue?: string;
  comparisonValueLabel?: string;
  warnings?: string[];
  url?: string;
  filingId?: string;
  sourceFactId?: string;
  documentId?: string;
  reportingDate?: string;
  filedDate?: string;
  postedDate?: string;
  issuedDate?: string;
  effectiveDate?: string;
  retrievedDate?: string;
  firstSeen?: string;
  lineagePath?: string;
  lineageObservationId?: string;
};

export type BackendQuality = {
  availability: string;
  origin: string;
  method: string;
  version_status: string;
  validation: string;
  qa_flags: string | null;
  review_status: string | null;
  missing_reason: string | null;
  applicability_evidence: string | null;
};

export type BackendPeriod = {
  basis: string;
  label: string;
  reporting_period: string | null;
  reporting_year: number | null;
  start: string | null;
  end: string | null;
  instant: string | null;
};

export type BackendValue = {
  as_filed: string | null;
  numeric: number | null;
  normalized_iso: string | null;
  unit: string | null;
  display_value: number | string | null;
  display_unit: string | null;
  display_scale: number | null;
};

export type BackendComparison = {
  eligible: boolean;
  key: string | null;
  group_id: string | null;
  series_id: string | null;
  subject_id: string | null;
  comparison_value_base: number | null;
  base_unit_family: string | null;
  scope_contract: string | null;
  reasons: string[];
};

export type BackendSource = {
  system: string;
  filingId: string | null;
  filingRef: string | null;
  accession: string | null;
  sourceFactId: string | null;
  contextId: string | null;
  documentId: string | null;
  concept: string | null;
  schedulePage: string | null;
  selector: string | null;
  taxonomyVersion: string | null;
  form: string | null;
  canonical: boolean | null;
  canonicalReason: string | null;
  versionStatus: string | null;
  acceptanceStatus: string | null;
  dataOrigin: string | null;
  url: string | null;
  documentTitle: string | null;
  documentHash: string | null;
  fact: {
    valueAsFiled: string | null;
    unitText: string | null;
    decimals: string | null;
    isNil: boolean;
  } | null;
  assertions: {
    id: string;
    documentId: string | null;
    entityKey: string | null;
    filingId: string | null;
    firstSeen: string | null;
    metricId: string | null;
    sourceFactId: string | null;
    sourceSystem: string | null;
    type: string;
    confidence: string;
    contentHash: string;
    extractionMethod: string;
    page: string | null;
    paragraph: string | null;
    charStart: number | null;
    charEnd: number | null;
    value: number | string | null;
    unit: string | null;
    qualifier: string | null;
    scopeNote: string | null;
    reviewState: string | null;
    reviewerNote: string | null;
    verbatimSpan: string | null;
  }[];
};

export type BackendObservation = {
  id: string;
  period: BackendPeriod;
  sortKey: string;
  value: BackendValue;
  quality: BackendQuality;
  scope: {
    actual: string;
    contract_rule: string | null;
    resolved: boolean;
  };
  dates: Record<string, string | null>;
  comparison: BackendComparison;
  sourceRegime: string;
  notes: string | null;
  source: BackendSource;
  lineage: {
    derivation: string | null;
    edgeCount: number;
    edgeSample: Record<string, unknown>[];
    edgeSampleComplete: boolean;
    populationCount: number;
    populationSample: Record<string, unknown>[];
    populationSampleComplete: boolean;
    fullPath: string | null;
  } | null;
};

export type BackendMetricSeries = {
  id: string;
  label: string;
  description: string | null;
  role: string | null;
  unitFamily: string | null;
  canonicalUnit: string | null;
  configuredDisplayUnit: string | null;
  pointCount: number;
  presentCount: number;
  reviewCount: number;
  latest: BackendObservation | null;
  points: BackendObservation[];
};

export type BackendDataSummary = {
  observations: number;
  metrics: number;
  present: number;
  usable: number;
  review?: number;
  comparison_eligible_observations?: number;
  comparison_eligible_series?: number;
  history_from: string | null;
  history_to: string | null;
};

export type ComparisonGroupSummary = {
  groupId: string;
  metricCount: number;
  seriesCount: number;
  periodFingerprints: string[];
};

export type OperatingAssetSummary = {
  id: string;
  name: string;
  company: string;
  ticker: string;
  legalFiler: string;
  cid: string | null;
  entityKey: string | null;
  regime: string;
  assetType: string;
  canonicalAssetType: string;
  canonicalAssetTypeLabel: string;
  authority: string;
  statusLabel: string;
  statusNote: string | null;
  inScope: boolean;
  exclusionReason: string | null;
  scopeRelation: string;
  scopeNote: string;
  comparisonEligible: boolean;
  comparisonBlockedReason: string | null;
  comparisonGroupIds: string[];
  comparisonGroups: ComparisonGroupSummary[];
  comparisonSubjectIds: string[];
  dataStatus: string;
  dataSummary: BackendDataSummary | null;
  reportsForms: string[];
  snapshotDate: string;
  latestPeriod: string | null;
  lastFiled: string | null;
  latestMetric: {
    metricId: string;
    label: string;
    period: string;
    value: number | string | null;
    unit: string | null;
    availability: string;
    validation: string;
  } | null;
  reviewCount: number;
  detailPath: string;
  relatedAssetIds: string[];
  relatedProjectIds: string[];
  relatedProjectMappingStatus: string;
  interests: {
    basis: string;
    effective_from: string | null;
    effective_to: string | null;
    parent: string;
    pct: number | null;
    qualifier: string;
    ticker: string;
  }[];
  interestDisplay: string | null;
  dockets: string[];
  groupKey: string | null;
  codGroup: string | null;
  note: string | null;
};

export type BackendAnnotation = {
  applied_count: number;
  entity_key: string;
  evidence_hash: string;
  evidence_ref: string;
  filed_text: string;
  filing_id: string;
  metric_id: string;
  rationale: string;
  review_status: string;
  reviewed_at: string;
  reviewer: string;
  source_fact_id: string;
  source_system: string;
};

export type OperatingAssetDetail = {
  schema: 'ferc_site_snapshot_v1';
  generationId: string;
  contractVersion: string;
  asOf: string;
  asset: OperatingAssetSummary;
  entity: Record<string, unknown> | null;
  entityScopeRelation: string;
  summary: Record<string, number | string | null> | null;
  metrics: BackendMetricSeries[];
  events: ChangeItem[];
  annotations: BackendAnnotation[];
};

export type OperatingInstrumentSummary = {
  id: string;
  entityKey: string;
  name: string;
  jurisdiction: string | null;
  note: string | null;
  dataSummary: BackendDataSummary;
  latestPeriod: string | null;
  reviewCount: number;
  detailPath: string;
};

export type OperatingInstrumentDetail = {
  schema: 'ferc_site_snapshot_v1';
  generationId: string;
  contractVersion: string;
  asOf: string;
  instrument: OperatingInstrumentSummary;
  entity: Record<string, unknown>;
  summary: BackendDataSummary;
  metrics: BackendMetricSeries[];
  annotations: BackendAnnotation[];
};

export type QuarterPoint = {
  period: string;
  revenue: number | null;
  income: number | null;
  throughput: number | null;
  sourceId: string;
  warning?: string;
};

export type AnnualPoint = {
  year: string;
  revenue: number;
  income: number;
};

export type CanonicalAssetType =
  | 'interstate_gas_transmission'
  | 'dedicated_gas_storage'
  | 'intrastate_hinshaw_services'
  | 'lng_facility'
  | 'crude_oil_pipeline'
  | 'refined_products_pipeline'
  | 'ngl_pipeline';

export type OperatingAssetView = {
  id: string;
  name: string;
  company: string;
  ticker: string;
  legalFiler: string;
  cid: string;
  regime: 'Interstate gas';
  canonicalAssetType?: CanonicalAssetType;
  canonicalAssetTypeLabel?: string;
  canonicalAssetTypeWarning?: string;
  scope: string;
  snapshotDate: string;
  latestCopiedQuarter: string;
  quarters: QuarterPoint[];
  annual: AnnualPoint[];
  status: DataStatus;
  changes: { date: string; title: string; detail: string; sourceId: string }[];
  sources: SourceDetail[];
};

export type ProjectMilestone = {
  accession: string;
  date: string;
  event_type: string;
  title: string;
  description: string;
};

export type ProjectActivity = {
  activity_type: string;
  label: string;
  start_date: string;
  end_date: string;
  filing_count: number;
  event_types: Record<string, number>;
  relationship: string;
  representative_accessions: string[];
  summary: string;
};

export type RegulatoryOutlook = {
  regulatory_status: string;
  tracked_ferc_requests: number;
  responded_ferc_requests: number;
  open_ferc_requests: number;
  latest_material_milestone: ProjectMilestone | null;
  schedule_watch: {
    kind: string;
    request_accession: string;
    request_date: string;
    estimated_due_date: string;
    response_date: string;
    detail: string;
  }[];
  progression_counts: Record<string, number>;
  next_expected_activity: string;
  investor_summary: string;
};

export type ProjectView = {
  project: string;
  docket: string;
  current_stage: string;
  generated_at: string;
  company: 'Company not mapped';
  counts: {
    raw: number;
    deduped: number;
    alerts: number;
    review: number;
    suppressed: number;
    activity_groups: number;
  };
  milestones: ProjectMilestone[];
  regulatory_outlook: RegulatoryOutlook | null;
  activity: ProjectActivity[];
};

export type ChangeItem = {
  id: string;
  kind: 'substantive' | 'archive' | 'review';
  domain: 'Operating Assets' | 'Projects';
  category: string;
  date: string;
  filedDate?: string;
  postedDate?: string;
  reportingPeriod?: string;
  effectiveDate?: string;
  firstSeen?: string;
  title: string;
  entity: string;
  company: string;
  regime: string;
  explanation: string;
  source: SourceDetail;
  filingChildren?: { label: string; value: string }[];
  assetIds?: string[];
  destination?: string;
  isBackfill?: boolean;
  companies?: string[];
};
