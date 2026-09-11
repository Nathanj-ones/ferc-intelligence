import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import { selectAssetKeyMetrics } from '../lib/ferc/key-metrics.ts';
import {
  concentrationComparisonKey,
  requiresQuantityDenominator,
} from '../lib/ferc/metric-presentation.ts';

const EXPECTED_GENERATION =
  process.env.FERC_EXPECTED_GENERATION ||
  '0dccbd426f15372f1330737537f9aac58bc9547a2eaf81ef6f4b655f10cf2824';
assert.match(EXPECTED_GENERATION, /^[a-f0-9]{64}$/, 'Invalid expected generation');
const EXPECTED_SCHEMA = 'ferc_operating_assets_frontend_v1';
const EXPECTED_CONTRACT_VERSION = '1.1.0';
const SNAPSHOT_SCHEMA = 'ferc_site_snapshot_v1';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDirectory, '..');
const defaultBackendRoot =
  '/Users/nathanjones/Desktop/ferc_reaudit_handoff/operating_assets_backend_ready_20260910/operating_assets_all_regimes';
const backendRoot = path.resolve(
  process.env.FERC_BACKEND_ROOT || defaultBackendRoot,
);
const fercDataRoot = path.resolve(
  process.env.FERC_FRONTEND_DATA_ROOT ||
    path.join(projectRoot, 'public', 'data', 'ferc'),
);
const outputParent = path.join(fercDataRoot, 'generations');
const liveOutputRoot = path.join(outputParent, EXPECTED_GENERATION);
fs.mkdirSync(outputParent, { recursive: true });
const outputRoot = fs.mkdtempSync(path.join(outputParent, '.v1-sync-'));
const incompleteLineageIds = new Set();
const reviewValidations = new Set([
  'source_anomaly_review',
  'blocked_ambiguity',
  'scope_incompatible',
  'unit_warning',
  'rounding_warning',
  'source_date_warning',
]);

const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
const jsonBytes = (value) => Buffer.from(`${JSON.stringify(value)}\n`);

function readCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        field += character;
      }
    } else if (character === '"') {
      quoted = true;
    } else if (character === ',') {
      row.push(field);
      field = '';
    } else if (character === '\n') {
      row.push(field.replace(/\r$/, ''));
      if (row.some((value) => value !== '')) rows.push(row);
      row = [];
      field = '';
    } else {
      field += character;
    }
  }
  if (quoted) throw new Error('Unterminated quoted CSV field.');
  if (field || row.length) {
    row.push(field.replace(/\r$/, ''));
    rows.push(row);
  }
  const [header, ...body] = rows;
  return body.map((values) =>
    Object.fromEntries(
      header.map((name, index) => [name, values[index] ?? '']),
    ),
  );
}

function safeChild(root, relativePath) {
  assert.equal(
    path.isAbsolute(relativePath),
    false,
    `Absolute path rejected: ${relativePath}`,
  );
  const resolved = path.resolve(root, relativePath);
  const prefix = `${path.resolve(root)}${path.sep}`;
  assert.ok(
    resolved.startsWith(prefix),
    `Path traversal rejected: ${relativePath}`,
  );
  if (fs.existsSync(resolved)) {
    const realRoot = fs.realpathSync(root);
    const realResolved = fs.realpathSync(resolved);
    assert.ok(
      realResolved.startsWith(`${realRoot}${path.sep}`),
      `Symlink escape rejected: ${relativePath}`,
    );
  }
  return resolved;
}

function titleCaseIdentifier(value) {
  return value
    .split('_')
    .map((word) => (word ? `${word[0].toUpperCase()}${word.slice(1)}` : word))
    .join(' ')
    .replace(/\bIoc\b/g, 'IOC')
    .replace(/\bLng\b/g, 'LNG')
    .replace(/\bMdq\b/g, 'MDQ')
    .replace(/\bWacc\b/g, 'WACC');
}

const assetTypeLabels = {
  gas_storage: 'Gas storage',
  interstate_gas: 'Interstate gas',
  intrastate_549d: 'Intrastate / Hinshaw',
  liquids: 'Liquids pipeline',
  lng: 'LNG facility',
  out_of_template: 'Outside operating templates',
  OUT_OF_TEMPLATE: 'Outside operating templates',
};

function periodSortKey(observation) {
  const period = observation.period || {};
  const dates = observation.dates || {};
  return (
    period.instant ||
    period.end ||
    dates.source_reporting_instant ||
    dates.source_reporting_end ||
    observation.value?.normalized_iso ||
    `${String(period.reporting_year || '').padStart(4, '0')}-${period.reporting_period || ''}`
  );
}

function compactAssertion(assertionId, sourceIndex) {
  const assertion = sourceIndex.document_assertions?.[assertionId];
  assert.ok(assertion, `Unknown document assertion: ${assertionId}`);
  return {
    id: assertionId,
    documentId: assertion.document_id || null,
    entityKey: assertion.entity_key || null,
    filingId: assertion.filing_id || null,
    firstSeen: assertion.first_seen_at || null,
    metricId: assertion.metric_id || null,
    sourceFactId: assertion.source_fact_id || null,
    sourceSystem: assertion.source_system || null,
    type: assertion.assertion_type,
    confidence: assertion.confidence,
    contentHash: assertion.content_hash,
    extractionMethod: assertion.extraction_method,
    page: assertion.page,
    paragraph: assertion.paragraph,
    charStart: assertion.char_start,
    charEnd: assertion.char_end,
    value: assertion.value_num ?? assertion.value_text ?? null,
    unit: assertion.unit || null,
    qualifier: assertion.qualifier || null,
    scopeNote: assertion.scope_note || null,
    reviewState: assertion.review_state || null,
    reviewerNote: assertion.reviewer_note || null,
    verbatimSpan: assertion.verbatim_span || null,
  };
}

function compactObservation(observation, sourceIndex) {
  const source = observation.source || {};
  const filing = source.filing_ref
    ? sourceIndex.filings?.[source.filing_ref]
    : null;
  if (source.filing_ref) {
    assert.ok(filing, `Unknown filing reference: ${source.filing_ref}`);
  }
  const document = source.document_id
    ? sourceIndex.documents?.[source.document_id]
    : null;
  if (source.document_id) {
    assert.ok(document, `Unknown document reference: ${source.document_id}`);
  }
  const assertionIds = source.document_assertion_refs || [];
  const canonicalValue = source.filing?.canonical ?? filing?.is_canonical;
  const sourceUrl =
    source.document?.resolved_url ||
    source.document?.source_url ||
    document?.source_url ||
    source.filing?.source_url ||
    filing?.source_url ||
    null;
  return {
    id: observation.observation_id,
    period: observation.period,
    sortKey: periodSortKey(observation),
    value: observation.value,
    quality: observation.quality,
    scope: observation.scope,
    dates: observation.dates,
    comparison: observation.comparison,
    sourceRegime: observation.source_regime,
    notes: observation.notes,
    source: {
      system: source.system,
      filingId: source.filing_id,
      filingRef: source.filing_ref,
      accession: source.accession_number || filing?.accession_number || null,
      sourceFactId: source.source_fact_id,
      contextId: source.context_id,
      documentId: source.document_id,
      concept: source.concept_qname || source.concept_local || null,
      schedulePage: source.schedule_page || null,
      selector: source.selector || null,
      taxonomyVersion:
        source.taxonomy_version || filing?.taxonomy_version || null,
      form: source.filing?.form || filing?.form || null,
      canonical:
        canonicalValue === null || canonicalValue === undefined
          ? null
          : canonicalValue === true ||
            canonicalValue === 1 ||
            canonicalValue === '1',
      canonicalReason: filing?.canonical_reason || null,
      versionStatus: filing?.version_status || null,
      acceptanceStatus: filing?.acceptance_status || null,
      dataOrigin: filing?.data_origin || null,
      url: safeFercUrl(sourceUrl),
      documentTitle: source.document?.title || document?.title || null,
      documentHash:
        source.document?.content_hash || document?.content_hash || null,
      fact: source.fact
        ? {
            valueAsFiled: source.fact.value_as_filed,
            unitText: source.fact.unit_text,
            decimals: source.fact.decimals,
            isNil: source.fact.is_nil,
          }
        : null,
      assertions: assertionIds.map((id) => compactAssertion(id, sourceIndex)),
    },
    lineage: observation.lineage
      ? (() => {
          const incomplete =
            !observation.lineage.edge_sample_complete ||
            !observation.lineage.population_sample_complete;
          if (incomplete) incompleteLineageIds.add(observation.observation_id);
          return {
            derivation: observation.lineage.derivation,
            edgeCount: observation.lineage.edge_count,
            edgeSample: observation.lineage.edge_sample,
            edgeSampleComplete: observation.lineage.edge_sample_complete,
            populationCount: observation.lineage.population_count,
            populationSample: observation.lineage.population_sample,
            populationSampleComplete:
              observation.lineage.population_sample_complete,
            fullPath: incomplete
              ? `/data/ferc/generations/${EXPECTED_GENERATION}/provenance/lineage-${observation.observation_id.slice(4, 6)}.json.gz`
              : null,
          };
        })()
      : null,
  };
}

function metricSeries(observations, metricRegistry) {
  const groups = new Map();
  for (const observation of observations) {
    const group = groups.get(observation.metric_id) || [];
    group.push(observation);
    groups.set(observation.metric_id, group);
  }
  return [...groups.entries()]
    .map(([metricId, points]) => {
      const registry = metricRegistry.get(metricId) || {};
      const projected = points
        .map((point) => compactObservation(point, sourceIndex))
        .sort((left, right) => left.sortKey.localeCompare(right.sortKey));
      const present = projected.filter(
        (point) => point.quality?.availability === 'present',
      );
      const currentPresent = present.filter(
        (point) => point.quality?.version_status !== 'superseded',
      );
      const usable = present.filter(
        (point) =>
          point.quality?.validation === 'pass' &&
          point.quality?.version_status !== 'superseded' &&
          (point.value?.display_value !== null ||
            point.value?.normalized_iso ||
            point.value?.as_filed),
      );
      const usableSeries = new Set(
        usable.map(
          (point) =>
            point.comparison?.series_id ||
            [
              point.scope?.actual,
              point.period?.basis,
              point.value?.display_unit || point.value?.unit,
            ].join('|'),
        ),
      );
      return {
        id: metricId,
        label: registry.display || titleCaseIdentifier(metricId),
        description: registry.meaning || null,
        role: registry.role || null,
        unitFamily: registry.unit_family || null,
        canonicalUnit: registry.canonical_unit || null,
        configuredDisplayUnit: registry.display_unit || null,
        pointCount: projected.length,
        presentCount: present.length,
        reviewCount: currentPresent.filter((point) =>
          reviewValidations.has(point.quality?.validation),
        ).length,
        openReviewCount: currentPresent.filter(
          (point) => point.quality?.review_status === 'open',
        ).length,
        resolvedReviewCount: currentPresent.filter(
          (point) => point.quality?.review_status === 'resolved',
        ).length,
        latest: usableSeries.size === 1 ? usable.at(-1) || null : null,
        points: projected,
      };
    })
    .sort((left, right) => {
      const roleDifference =
        Number(right.role === 'headline') - Number(left.role === 'headline');
      if (roleDifference) return roleDifference;
      const presenceDifference = right.presentCount - left.presentCount;
      return presenceDifference || left.label.localeCompare(right.label);
    });
}

function comparisonPeriodKey(point) {
  const period = point.period || {};
  return [
    period.basis,
    period.start,
    period.end,
    period.instant,
    period.label,
  ].join('|');
}

function comparisonGroupSummaries(metrics) {
  const groups = new Map();
  for (const metric of metrics) {
    // The pinned backend tags the rendered horsepower column as utr:MW and
    // does not propagate that anomaly to every comparison point. Keep the
    // observations visible, but fail closed for cross-asset comparison.
    if (metric.id === 'certificated_horsepower') continue;
    for (const point of metric.points) {
      const comparison = point.comparison;
      if (
        ((metric.id === 'liq_barrel_miles' ||
          metric.id === 'p700_barrel_miles') &&
          (point.value?.display_unit || point.value?.unit)
            ?.trim()
            .toLowerCase() === 'utr:bbl' &&
          point.quality?.validation === 'unit_warning') ||
        comparison?.eligible !== true ||
        point.quality?.review_status === 'open' ||
        !comparison.group_id ||
        typeof comparison.comparison_value_base !== 'number' ||
        !Number.isFinite(comparison.comparison_value_base)
      ) {
        continue;
      }
      const group = groups.get(comparison.group_id) || {
        metricIds: new Set(),
        seriesIds: new Set(),
        periodKeys: new Set(),
        semanticKeys: new Set(),
      };
      group.metricIds.add(metric.id);
      if (requiresQuantityDenominator(metric.id)) {
        group.semanticKeys.add(
          concentrationComparisonKey([point]) || 'ambiguous',
        );
      }
      if (comparison.series_id) group.seriesIds.add(comparison.series_id);
      const periodKey = comparisonPeriodKey(point);
      if (periodKey !== '||||') group.periodKeys.add(periodKey);
      groups.set(comparison.group_id, group);
    }
  }
  return [...groups.entries()]
    .map(([groupId, group]) => ({
      groupId,
      metricCount: group.metricIds.size,
      seriesCount: group.seriesIds.size,
      semanticKey:
        group.semanticKeys.size === 1
          ? [...group.semanticKeys][0]
          : group.semanticKeys.size > 1
            ? 'ambiguous'
            : null,
      periodFingerprints: [...group.periodKeys]
        .map((periodKey) => sha256(Buffer.from(periodKey)).slice(0, 16))
        .sort((left, right) => left.localeCompare(right)),
    }))
    .sort((left, right) => left.groupId.localeCompare(right.groupId));
}

function directoryPresentationValue(point) {
  const value = point?.value || {};
  if (value.display_value !== null && value.display_value !== undefined) {
    return value.display_value;
  }
  const normalizedUnit = (value.display_unit || value.unit || '')
    .trim()
    .toLowerCase();
  if (['date', '(date)', '(date range)'].includes(normalizedUnit)) {
    return value.normalized_iso || value.as_filed || null;
  }
  return value.as_filed || value.normalized_iso || null;
}

function reviewSummary(metrics) {
  const points = metrics
    .flatMap((metric) => metric.points)
    .filter(
      (point) =>
        point.quality?.availability === 'present' &&
        point.quality?.version_status !== 'superseded',
    );
  return {
    qualityFlags: points.filter((point) =>
      reviewValidations.has(point.quality?.validation),
    ).length,
    open: points.filter((point) => point.quality?.review_status === 'open')
      .length,
    resolved: points.filter(
      (point) => point.quality?.review_status === 'resolved',
    ).length,
  };
}

function latestAvailablePeriod(metrics) {
  const candidates = metrics
    .flatMap((metric) => metric.points)
    .filter(
      (point) =>
        point.quality?.availability === 'present' &&
        point.quality?.validation === 'pass' &&
        point.quality?.version_status !== 'superseded' &&
        directoryPresentationValue(point) !== null,
    )
    .sort((left, right) => left.sortKey.localeCompare(right.sortKey));
  const latest = candidates.at(-1);
  if (!latest) return null;
  return (
    latest.period?.instant ||
    latest.period?.end ||
    latest.dates?.source_reporting_instant ||
    latest.dates?.source_reporting_end ||
    (/^\d{4}-\d{2}-\d{2}/.test(latest.sortKey)
      ? latest.sortKey.slice(0, 10)
      : null)
  );
}

function eventDate(event) {
  const candidates = [
    event.effective_date,
    event.reporting_date,
    event.source_filed_date,
    event.source_posted_date,
    event.first_seen_at,
  ].filter(Boolean);
  return (
    candidates.find((value) => !/^9\d{3}-/.test(value)) || candidates[0] || ''
  );
}

function safeFercUrl(raw) {
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
    // Invalid and non-public URLs are omitted from the browser-facing snapshot.
  }
  return undefined;
}

function compactEvent(event, entity, assetById) {
  const referencedAssets = (event.asset_ids || [])
    .map((id) => assetById.get(id))
    .filter(Boolean);
  const representative = referencedAssets[0];
  const companies = [
    ...new Set(
      referencedAssets
        .map((asset) => asset.company)
        .filter((company) => company && company !== 'Company not mapped'),
    ),
  ].sort((left, right) => left.localeCompare(right));
  const filingRef = event.source?.filing_ref || event.filing_ref || null;
  const filing = filingRef ? sourceIndex.filings?.[filingRef] : null;
  let kind =
    event.destination === 'data_review_queue'
      ? 'review'
      : event.destination === 'investor_feed'
        ? 'substantive'
        : 'archive';
  if (event.is_backfill && kind === 'substantive') kind = 'archive';
  const warnings = [];
  if (event.is_backfill) {
    warnings.push(
      'Historical backfill: this event is not current investor news.',
    );
  }
  if (event.confidence_note) warnings.push(event.confidence_note);
  if (
    event.source_resolution_status &&
    event.source_resolution_status !== 'resolved'
  ) {
    warnings.push(`Source resolution: ${event.source_resolution_status}.`);
  }
  if (/^9\d{3}-/.test(event.effective_date || '')) {
    warnings.push(
      `Filed effective-date sentinel ${event.effective_date} is retained in evidence but not used to sort the archive.`,
    );
  }
  return {
    id: `operating-${event.event_id}`,
    kind,
    domain: 'Operating Assets',
    category: titleCaseIdentifier(
      event.event_type || event.event_class || 'filing',
    ),
    date: eventDate(event).slice(0, 10),
    filedDate: event.source_filed_date || undefined,
    postedDate: event.source_posted_date || undefined,
    reportingPeriod: event.reporting_date || undefined,
    effectiveDate: event.effective_date || undefined,
    firstSeen: event.first_seen_at || undefined,
    title:
      event.headline || titleCaseIdentifier(event.event_type || 'FERC filing'),
    entity:
      referencedAssets.length > 0
        ? `${referencedAssets.map((asset) => asset.name).join(', ')} · ${entity.legal_name}`
        : entity.legal_name,
    company:
      companies.length > 1
        ? companies.join(', ')
        : companies[0] ||
          representative?.company ||
          entity.parent ||
          entity.ticker ||
          'Company not mapped',
    companies,
    regime:
      representative?.regime || entity.jurisdiction || 'FERC operating record',
    explanation: event.detail || 'FERC event retained in the backend archive.',
    assetIds: referencedAssets.map((asset) => asset.id),
    destination: event.destination,
    isBackfill: Boolean(event.is_backfill),
    source: {
      id: event.document_id || event.filing_ref || event.event_id,
      title:
        event.headline ||
        titleCaseIdentifier(event.event_type || 'FERC filing'),
      sourceSystem: event.source_system || event.source?.system || 'FERC',
      nativeIdentity: [filingRef, event.document_id]
        .filter(Boolean)
        .join(' · '),
      accession:
        event.accession_number || event.source?.accession_number || undefined,
      docket: event.docket || undefined,
      filingVersion: filing?.version_status || 'Unresolved occurrence version',
      canonicalStatus:
        filing?.is_canonical === undefined
          ? 'Unknown'
          : filing.is_canonical
            ? 'Canonical occurrence'
            : 'Noncanonical occurrence',
      canonicalReason: filing?.canonical_reason || undefined,
      acceptanceStatus: filing?.acceptance_status || undefined,
      dataOrigin: filing?.data_origin || undefined,
      period: event.reporting_date || event.effective_date || undefined,
      scope: entity.legal_name,
      method: `Backend event classification · ${event.destination}`,
      availability:
        event.source_resolution_status === 'resolved'
          ? 'Present'
          : event.source_resolution_status || 'Recorded',
      origin: event.source_system || event.source?.system || 'FERC',
      validation: kind === 'review' ? 'Under review' : 'Archived backfill',
      description: event.detail || undefined,
      warnings,
      url: safeFercUrl(
        event.source_url ||
          event.source?.resolved_url ||
          event.source?.document_url ||
          event.source?.filing_url,
      ),
      filedDate: event.source_filed_date || undefined,
      postedDate: event.source_posted_date || undefined,
      reportingDate: event.reporting_date || undefined,
      issuedDate: filing?.issued_date || undefined,
      effectiveDate: event.effective_date || undefined,
      retrievedDate: filing?.retrieved_at || undefined,
      firstSeen: event.first_seen_at || undefined,
      filingId: event.filing_id || filing?.filing_id || undefined,
    },
  };
}

const receiptPath = path.join(backendRoot, 'publication_receipt.json');
assert.ok(
  fs.existsSync(receiptPath),
  `Backend receipt not found: ${receiptPath}`,
);
const receipt = readJson(receiptPath);
assert.equal(
  receipt.generation_id,
  EXPECTED_GENERATION,
  'Unexpected backend generation.',
);
assert.equal(receipt.metadata?.frontend_contract?.schema, EXPECTED_SCHEMA);
assert.equal(
  receipt.metadata?.frontend_contract?.contract_version,
  EXPECTED_CONTRACT_VERSION,
);
assert.match(
  receipt.generation_path,
  /^\.generations\/consumer_exports\/[a-f0-9]{64}$/,
  'Unsafe generation path in receipt.',
);
assert.ok(receipt.generation_path.endsWith(EXPECTED_GENERATION));

const generationDirectory = safeChild(backendRoot, receipt.generation_path);
const generationManifest = readJson(
  path.join(generationDirectory, 'GENERATION_MANIFEST.json'),
);
assert.deepEqual(
  generationManifest.files,
  receipt.files,
  'Receipt/manifest file maps differ.',
);

const verifiedInputs = new Map();
function verifiedFile(relativePath) {
  if (verifiedInputs.has(relativePath)) return verifiedInputs.get(relativePath);
  const expected = receipt.files?.[relativePath];
  assert.ok(expected, `File is outside the receipt: ${relativePath}`);
  const file = safeChild(generationDirectory, relativePath);
  const bytes = fs.readFileSync(file);
  assert.equal(
    bytes.length,
    expected.bytes,
    `Byte-size mismatch: ${relativePath}`,
  );
  assert.equal(
    sha256(bytes),
    expected.sha256,
    `SHA-256 mismatch: ${relativePath}`,
  );
  verifiedInputs.set(relativePath, bytes);
  return bytes;
}

const contract = JSON.parse(verifiedFile('frontend_v1/contract.json'));
assert.equal(contract.schema, EXPECTED_SCHEMA);
assert.equal(contract.contract_version, EXPECTED_CONTRACT_VERSION);
assert.equal(
  contract.integration_mode,
  'read_only_export_adapter; never open the frontend app database',
);

const directorySource = JSON.parse(verifiedFile('frontend_v1/assets.json'));
assert.equal(directorySource.schema, EXPECTED_SCHEMA);
assert.equal(directorySource.contract_version, EXPECTED_CONTRACT_VERSION);
assert.equal(directorySource.as_of, receipt.metadata.as_of);

const headlineAvailability = JSON.parse(
  verifiedFile('frontend_v1/headline_availability.json'),
);
assert.equal(headlineAvailability.schema, EXPECTED_SCHEMA);
assert.equal(headlineAvailability.contract_version, EXPECTED_CONTRACT_VERSION);
const instrumentsSource = JSON.parse(
  verifiedFile('frontend_v1/instruments.json'),
);
assert.ok(Array.isArray(instrumentsSource));

const sourceIndex = JSON.parse(verifiedFile('frontend_v1/source_index.json'));
assert.equal(sourceIndex.schema, `${EXPECTED_SCHEMA}_source_index`);
assert.equal(sourceIndex.contract_version, EXPECTED_CONTRACT_VERSION);

const declaredDependencyNames = Object.values(
  contract.external_csv_dependencies,
).map((relativePath) => {
  assert.match(relativePath, /^\.\.\/[a-z_]+\.csv$/);
  return relativePath.slice(3);
});
const dependencyInputs = new Map(
  declaredDependencyNames.map((name) => [name, verifiedFile(name)]),
);
assert.equal(
  dependencyInputs.size,
  9,
  'Every declared CSV dependency must be verified.',
);

const metricRows = readCsv(
  dependencyInputs.get('metric_registry.csv').toString('utf8'),
);
const metricRegistry = new Map(metricRows.map((row) => [row.metric_id, row]));

const companyNames = new Map(
  directorySource.companies.map((company) => [
    company.ticker,
    company.parent?.[0] || company.ticker,
  ]),
);
const assetIds = new Set();
const assetPayloadPaths = new Set();
const entityPayloadPaths = new Set();
const summaries = [];
const detailFiles = new Map();
const instrumentSummaries = [];
const instrumentDetailFiles = new Map();
const eventsById = new Map();
const annotationsById = new Map();

for (const rawAsset of directorySource.assets) {
  assert.match(
    rawAsset.id,
    /^[a-z0-9][a-z0-9-]*$/,
    `Unsafe asset slug: ${rawAsset.id}`,
  );
  assert.equal(
    assetIds.has(rawAsset.id),
    false,
    `Duplicate asset slug: ${rawAsset.id}`,
  );
  assetIds.add(rawAsset.id);
  const assetPayloadRelative = `frontend_v1/${rawAsset.detailPath}`;
  assetPayloadPaths.add(assetPayloadRelative);
  const assetPayload = JSON.parse(verifiedFile(assetPayloadRelative));
  assert.equal(assetPayload.schema, EXPECTED_SCHEMA);
  assert.equal(assetPayload.contract_version, EXPECTED_CONTRACT_VERSION);
  assert.equal(assetPayload.asset.id, rawAsset.id);
  const normalizeAsset = (asset) => ({
    ...asset,
    entityMappings: (asset.entityMappings || []).map((entityMapping) => ({
      ...entityMapping,
      entity_payload_path: entityMapping.entity_payload_path.replace(
        /^\.\.\//,
        '',
      ),
    })),
  });
  assert.deepEqual(
    normalizeAsset(assetPayload.asset),
    normalizeAsset(rawAsset),
    `Asset payload differs from directory: ${rawAsset.id}`,
  );

  const mapping = rawAsset.entityMappings?.[0] || null;
  assert.ok(
    (rawAsset.entityMappings?.length || 0) <= 1,
    `Multiple entity mappings: ${rawAsset.id}`,
  );
  let entityPayload = null;
  if (mapping) {
    const entityPath = mapping.entity_payload_path.replace(/^\.\.\//, '');
    assert.match(entityPath, /^entity_payloads\/[A-Za-z0-9:_-]+\.json$/);
    const entityRelative = `frontend_v1/${entityPath}`;
    entityPayloadPaths.add(entityRelative);
    entityPayload = JSON.parse(verifiedFile(entityRelative));
    assert.equal(entityPayload.schema, EXPECTED_SCHEMA);
    assert.equal(entityPayload.contract_version, EXPECTED_CONTRACT_VERSION);
    assert.equal(entityPayload.entity.entity_key, mapping.entity_key);
    assert.ok(entityPayload.asset_ids.includes(rawAsset.id));
  }

  const projectedMetrics = entityPayload
    ? metricSeries(entityPayload.observations || [], metricRegistry)
    : [];
  const comparisonGroupIds = [
    ...new Set(
      projectedMetrics.flatMap((series) =>
        series.points
          .filter((point) => point.comparison?.eligible)
          .map((point) => point.comparison.group_id)
          .filter(Boolean),
      ),
    ),
  ].sort((left, right) => left.localeCompare(right));
  const comparisonSubjectIds = [
    ...new Set(
      projectedMetrics.flatMap((series) =>
        series.points
          .filter((point) => point.comparison?.eligible)
          .map((point) => point.comparison.subject_id)
          .filter(Boolean),
      ),
    ),
  ].sort((left, right) => left.localeCompare(right));
  const comparisonGroups = comparisonGroupSummaries(projectedMetrics);
  const reviewTriage = reviewSummary(projectedMetrics);
  const latestUsablePeriod = latestAvailablePeriod(projectedMetrics);
  const frontendComparisonEligible = Boolean(
    rawAsset.comparisonEligible &&
    comparisonGroups.some(
      (group) =>
        group.metricCount === 1 &&
        group.seriesCount === 1 &&
        group.periodFingerprints.length > 0,
    ),
  );
  const reviewCount = entityPayload?.summary?.review ?? 0;
  const company =
    rawAsset.parent ||
    companyNames.get(rawAsset.ticker) ||
    rawAsset.ticker ||
    'Company not mapped';
  const regime =
    rawAsset.id === 'oke-roadrunner-export-pipeline'
      ? 'Cross-border gas pipeline'
      : assetTypeLabels[rawAsset.assetType] ||
        titleCaseIdentifier(rawAsset.assetType);
  // The directory and asset page must choose the same safe, regime-specific
  // point, not whichever headline happens to sort first in the registry.
  const selectedHeadline = selectAssetKeyMetrics(
    { regime, scopeRelation: rawAsset.scopeRelation },
    projectedMetrics,
    1,
  )[0];
  const latestMetricSeries = selectedHeadline?.metric;
  const latestMetric = selectedHeadline?.point;
  const summary = {
    id: rawAsset.id,
    name: rawAsset.displayName,
    company,
    ticker: rawAsset.ticker || '—',
    legalFiler:
      mapping?.legal_name ||
      rawAsset.entityName ||
      'No mapped FERC filing entity',
    cid: mapping?.cid || rawAsset.cid || null,
    entityKey: mapping?.entity_key || null,
    regime,
    assetType: rawAsset.assetType,
    canonicalAssetType: rawAsset.assetType,
    canonicalAssetTypeLabel: regime,
    authority: rawAsset.authority,
    statusLabel: rawAsset.status,
    statusNote: rawAsset.statusNote,
    inScope: Boolean(rawAsset.inScope),
    exclusionReason: rawAsset.exclusionReason,
    scopeRelation: rawAsset.scopeRelation,
    scopeNote: assetPayload.scope_note,
    comparisonEligible: frontendComparisonEligible,
    comparisonBlockedReason: frontendComparisonEligible
      ? rawAsset.comparisonBlockedReason
      : rawAsset.comparisonBlockedReason ||
        'no_unambiguous_dated_comparison_series_in_snapshot',
    comparisonGroupIds,
    comparisonGroups,
    comparisonSubjectIds,
    dataStatus: rawAsset.dataStatus,
    dataSummary: rawAsset.dataSummary,
    reportsForms: rawAsset.reportsForms || [],
    snapshotDate: directorySource.as_of,
    latestPeriod: latestUsablePeriod,
    lastFiled: rawAsset.lastFiled,
    latestMetric: latestMetric
      ? {
          metricId: latestMetricSeries.id,
          label: selectedHeadline.definition.label || latestMetricSeries.label,
          period: latestMetric.period?.label || latestMetric.sortKey,
          value: directoryPresentationValue(latestMetric),
          unit: latestMetric.value?.display_unit || latestMetric.value?.unit,
          configuredDisplayUnit:
            latestMetricSeries.configuredDisplayUnit || null,
          displayScale: latestMetric.value?.display_scale ?? null,
          origin: latestMetric.quality?.origin || null,
          availability: latestMetric.quality?.availability,
          validation: latestMetric.quality?.validation,
        }
      : null,
    reviewCount,
    qualityFlagCount: reviewTriage.qualityFlags,
    openReviewCount: reviewTriage.open,
    resolvedReviewCount: reviewTriage.resolved,
    detailPath: `/data/ferc/generations/${EXPECTED_GENERATION}/assets/${rawAsset.id}.json.gz`,
    relatedAssetIds: rawAsset.relatedAssetIds || [],
    relatedProjectIds: [],
    relatedProjectMappingStatus: rawAsset.relatedProjectMappingStatus,
    interests: rawAsset.interests || [],
    interestDisplay: rawAsset.interestDisplay || null,
    dockets: rawAsset.dockets || [],
    groupKey: rawAsset.groupKey || null,
    codGroup: rawAsset.codGroup || null,
    note: rawAsset.note || null,
  };
  summaries.push(summary);

  const annotations = entityPayload?.reviewed_annotations || [];
  const detail = {
    schema: SNAPSHOT_SCHEMA,
    generationId: EXPECTED_GENERATION,
    contractVersion: EXPECTED_CONTRACT_VERSION,
    asOf: directorySource.as_of,
    asset: summary,
    entity: entityPayload?.entity || null,
    entityScopeRelation:
      entityPayload?.entity_scope_relation || rawAsset.scopeRelation,
    summary: entityPayload?.summary || rawAsset.dataSummary,
    metrics: projectedMetrics,
    events: (entityPayload?.events || []).map((event) => event.event_id),
    annotations,
  };
  detailFiles.set(rawAsset.id, detail);
}

const assetById = new Map(summaries.map((asset) => [asset.id, asset]));
for (const instrument of instrumentsSource) {
  const entityPath = instrument.entity_payload_path.replace(/^\.\.\//, '');
  assert.match(entityPath, /^entity_payloads\/[A-Za-z0-9:_-]+\.json$/);
  const entityRelative = `frontend_v1/${entityPath}`;
  entityPayloadPaths.add(entityRelative);
  const entityPayload = JSON.parse(verifiedFile(entityRelative));
  assert.equal(entityPayload.schema, EXPECTED_SCHEMA);
  assert.equal(entityPayload.contract_version, EXPECTED_CONTRACT_VERSION);
  assert.equal(entityPayload.entity.entity_key, instrument.entity_key);
  assert.deepEqual(entityPayload.summary, instrument.summary);
  const id = instrument.entity_key.toLowerCase();
  assert.match(id, /^[a-z0-9][a-z0-9-]*$/);
  const metrics = metricSeries(
    entityPayload.observations || [],
    metricRegistry,
  );
  const reviewTriage = reviewSummary(metrics);
  const summary = {
    id,
    entityKey: instrument.entity_key,
    name: instrument.legal_name,
    jurisdiction: entityPayload.entity.jurisdiction || null,
    note: entityPayload.entity.note || null,
    dataSummary: instrument.summary,
    latestPeriod: latestAvailablePeriod(metrics),
    reviewCount: instrument.summary.review ?? 0,
    qualityFlagCount: reviewTriage.qualityFlags,
    openReviewCount: reviewTriage.open,
    resolvedReviewCount: reviewTriage.resolved,
    detailPath: `/data/ferc/generations/${EXPECTED_GENERATION}/instruments/${id}.json.gz`,
  };
  instrumentSummaries.push(summary);
  instrumentDetailFiles.set(id, {
    schema: SNAPSHOT_SCHEMA,
    generationId: EXPECTED_GENERATION,
    contractVersion: EXPECTED_CONTRACT_VERSION,
    asOf: directorySource.as_of,
    instrument: summary,
    entity: entityPayload.entity,
    summary: entityPayload.summary,
    metrics,
    annotations: entityPayload.reviewed_annotations || [],
  });
}
const entityFiles = [...entityPayloadPaths].sort((left, right) =>
  left.localeCompare(right),
);
for (const entityRelative of entityFiles) {
  const entityPayload = JSON.parse(verifiedFile(entityRelative));
  for (const annotation of entityPayload.reviewed_annotations || []) {
    const annotationId =
      annotation.annotation_id ||
      annotation.observation_id ||
      sha256(Buffer.from(JSON.stringify(annotation)));
    annotationsById.set(annotationId, annotation);
  }
  for (const event of entityPayload.events || []) {
    assert.equal(
      eventsById.has(event.event_id),
      false,
      `Duplicate event ID: ${event.event_id}`,
    );
    eventsById.set(
      event.event_id,
      compactEvent(event, entityPayload.entity, assetById),
    );
  }
}

const annotationCount = annotationsById.size;

for (const detail of detailFiles.values()) {
  detail.events = detail.events.map((id) => eventsById.get(id)).filter(Boolean);
}

const changes = [...eventsById.values()].sort((left, right) => {
  const dateDifference = right.date.localeCompare(left.date);
  return dateDifference || left.id.localeCompare(right.id);
});
assert.equal(
  changes.filter((change) => change.kind === 'substantive').length,
  0,
  'The pinned generation must not promote backfill into the current feed.',
);

const actualAssetPayloads = fs
  .readdirSync(path.join(generationDirectory, 'frontend_v1', 'asset_payloads'))
  .filter((name) => name.endsWith('.json'))
  .map((name) => `frontend_v1/asset_payloads/${name}`);
assert.deepEqual(
  [...assetPayloadPaths].sort((left, right) => left.localeCompare(right)),
  actualAssetPayloads.sort((left, right) => left.localeCompare(right)),
  'Asset route closure differs from assets.json.',
);
const actualEntityPayloads = fs
  .readdirSync(path.join(generationDirectory, 'frontend_v1', 'entity_payloads'))
  .filter((name) => name.endsWith('.json'))
  .map((name) => `frontend_v1/entity_payloads/${name}`);
assert.deepEqual(
  [...entityPayloadPaths].sort((left, right) => left.localeCompare(right)),
  actualEntityPayloads.sort((left, right) => left.localeCompare(right)),
  'Entity route closure differs from assets.json.',
);

assert.equal(summaries.length, 120);
assert.equal(summaries.filter((asset) => asset.inScope).length, 109);
assert.equal(annotationCount, 7);
assert.equal(changes.length, 1238);

fs.mkdirSync(path.join(outputRoot, 'assets'), { recursive: true });
const generated = new Map();
function writeContent(relativePath, content, { gzip = false } = {}) {
  const bytes = gzip ? gzipSync(content, { level: 9 }) : content;
  const file = safeChild(outputRoot, relativePath);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, bytes);
  generated.set(relativePath, {
    bytes: bytes.length,
    sha256: sha256(bytes),
    encoding: gzip ? 'gzip' : 'identity',
    contentBytes: content.length,
    contentSha256: sha256(content),
  });
}

function writeGenerated(relativePath, value, options) {
  writeContent(relativePath, jsonBytes(value), options);
}

for (const [name, bytes] of dependencyInputs) {
  writeContent(`provenance/dependencies/${name}.gz`, bytes, { gzip: true });
}
writeContent(
  'provenance/source_index.json.gz',
  verifiedFile('frontend_v1/source_index.json'),
  { gzip: true },
);

const expectedLineage = new Map();
for (const detail of detailFiles.values()) {
  for (const metric of detail.metrics) {
    for (const point of metric.points) {
      if (point.lineage?.fullPath) {
        expectedLineage.set(point.id, {
          edgeCount: point.lineage.edgeCount,
          populationCount: point.lineage.populationCount,
        });
      }
    }
  }
}
assert.deepEqual(
  [...expectedLineage.keys()].sort((left, right) => left.localeCompare(right)),
  [...incompleteLineageIds].sort((left, right) => left.localeCompare(right)),
);

const lineageShards = new Map();
function lineageEntry(observationId) {
  const bucket = observationId.slice(4, 6);
  const shard = lineageShards.get(bucket) || new Map();
  lineageShards.set(bucket, shard);
  const entry = shard.get(observationId) || { edges: [], populations: [] };
  shard.set(observationId, entry);
  return entry;
}
for (const observationId of incompleteLineageIds) lineageEntry(observationId);
for (const edge of readCsv(
  dependencyInputs.get('lineage_edges.csv').toString('utf8'),
)) {
  if (incompleteLineageIds.has(edge.observation_id)) {
    lineageEntry(edge.observation_id).edges.push(edge);
  }
}
for (const population of readCsv(
  dependencyInputs.get('lineage_populations.csv').toString('utf8'),
)) {
  if (incompleteLineageIds.has(population.observation_id)) {
    lineageEntry(population.observation_id).populations.push(population);
  }
}
for (const [bucket, entries] of [...lineageShards].sort(([left], [right]) =>
  left.localeCompare(right),
)) {
  for (const [observationId, entry] of entries) {
    const expected = expectedLineage.get(observationId);
    assert.equal(
      entry.edges.length,
      expected.edgeCount,
      `Incomplete edge lineage: ${observationId}`,
    );
    assert.equal(
      entry.populations.length,
      expected.populationCount,
      `Incomplete population lineage: ${observationId}`,
    );
  }
  writeGenerated(
    `provenance/lineage-${bucket}.json.gz`,
    {
      schema: `${SNAPSHOT_SCHEMA}_lineage`,
      generationId: EXPECTED_GENERATION,
      bucket,
      observations: Object.fromEntries(entries),
    },
    { gzip: true },
  );
}

const directory = {
  schema: SNAPSHOT_SCHEMA,
  generationId: EXPECTED_GENERATION,
  contractVersion: EXPECTED_CONTRACT_VERSION,
  asOf: directorySource.as_of,
  registryVersion: directorySource.registry_version,
  status: 'candidate_snapshot',
  notice:
    'Receipt-pinned FERC operating snapshot. “Live” in the source contract means stored usable data, not live-source freshness.',
  counts: {
    ...directorySource.counts,
    events: changes.length,
    currentEvents: 0,
    reviewEvents: changes.filter((change) => change.kind === 'review').length,
    archiveEvents: changes.filter((change) => change.kind === 'archive').length,
    annotations: annotationCount,
  },
  coverage: receipt.metadata.coverage,
  coverageDefinitions: headlineAvailability.definitions,
  templateCoverage: headlineAvailability.templates,
  largestUnmatchedCoreBuckets:
    headlineAvailability.largest_unmatched_core_buckets,
  companies: directorySource.companies,
  instruments: instrumentSummaries,
  assets: summaries.sort((left, right) => left.name.localeCompare(right.name)),
};
writeGenerated('directory.json', directory);
writeGenerated(
  'changes.json.gz',
  {
    schema: SNAPSHOT_SCHEMA,
    generationId: EXPECTED_GENERATION,
    asOf: directorySource.as_of,
    currentCount: 0,
    changes,
  },
  { gzip: true },
);
for (const [assetId, detail] of [...detailFiles].sort(([left], [right]) =>
  left.localeCompare(right),
)) {
  writeGenerated(`assets/${assetId}.json.gz`, detail, { gzip: true });
}
for (const [instrumentId, detail] of [...instrumentDetailFiles].sort(
  ([left], [right]) => left.localeCompare(right),
)) {
  writeGenerated(`instruments/${instrumentId}.json.gz`, detail, { gzip: true });
}

const manifest = {
  schema: SNAPSHOT_SCHEMA,
  generationId: EXPECTED_GENERATION,
  source: {
    contractSchema: EXPECTED_SCHEMA,
    contractVersion: EXPECTED_CONTRACT_VERSION,
    asOf: directorySource.as_of,
    registryVersion: directorySource.registry_version,
    publicationCodeVersion: receipt.metadata.code_version,
    publicationDatabaseIdentity: receipt.metadata.database_identity,
  },
  routes: {
    directory: `/data/ferc/generations/${EXPECTED_GENERATION}/directory.json`,
    changes: `/data/ferc/generations/${EXPECTED_GENERATION}/changes.json.gz`,
    assets: `/data/ferc/generations/${EXPECTED_GENERATION}/assets/<asset-slug>.json.gz`,
    instruments: `/data/ferc/generations/${EXPECTED_GENERATION}/instruments/<instrument-slug>.json.gz`,
    lineage: `/data/ferc/generations/${EXPECTED_GENERATION}/provenance/lineage-<bucket>.json.gz`,
    sourceIndex: `/data/ferc/generations/${EXPECTED_GENERATION}/provenance/source_index.json.gz`,
    dependencies: `/data/ferc/generations/${EXPECTED_GENERATION}/provenance/dependencies/<declared-file>.gz`,
  },
  counts: directory.counts,
  inputVerification: {
    receiptFiles: Object.keys(receipt.files).length,
    verifiedFiles: verifiedInputs.size,
    assetPayloads: assetPayloadPaths.size,
    entityPayloads: entityPayloadPaths.size,
    instruments: instrumentSummaries.length,
    sourceFilingsResolved: sourceIndex.counts.filings,
    sourceDocumentsResolved: sourceIndex.counts.documents,
    sourceAssertionsResolved: sourceIndex.counts.document_assertions,
    declaredDependenciesVerified: dependencyInputs.size,
    incompleteLineageObservationsSharded: incompleteLineageIds.size,
  },
  files: Object.fromEntries(
    [...generated].sort(([left], [right]) => left.localeCompare(right)),
  ),
};
const manifestBytes = jsonBytes(manifest);
fs.writeFileSync(path.join(outputRoot, 'manifest.json'), manifestBytes);

const backupRoot = path.join(outputParent, `.v1-backup-${process.pid}`);
try {
  if (fs.existsSync(liveOutputRoot)) fs.renameSync(liveOutputRoot, backupRoot);
  fs.renameSync(outputRoot, liveOutputRoot);
  if (fs.existsSync(backupRoot)) fs.rmSync(backupRoot, { recursive: true });
} catch (error) {
  if (!fs.existsSync(liveOutputRoot) && fs.existsSync(backupRoot)) {
    fs.renameSync(backupRoot, liveOutputRoot);
  }
  if (fs.existsSync(outputRoot)) fs.rmSync(outputRoot, { recursive: true });
  throw error;
}

const pointerPath = path.join(fercDataRoot, 'current.json');
const pointerTempPath = path.join(fercDataRoot, `.current-${process.pid}.json`);
fs.writeFileSync(
  pointerTempPath,
  jsonBytes({
    schema: `${SNAPSHOT_SCHEMA}_pointer`,
    generationId: EXPECTED_GENERATION,
    contractVersion: EXPECTED_CONTRACT_VERSION,
    root: `/data/ferc/generations/${EXPECTED_GENERATION}`,
  }),
);
fs.renameSync(pointerTempPath, pointerPath);

const legacyOutputRoot = path.join(fercDataRoot, 'v1');
if (fs.existsSync(legacyOutputRoot)) {
  const legacyManifestPath = path.join(legacyOutputRoot, 'manifest.json');
  const legacyManifest = fs.existsSync(legacyManifestPath)
    ? readJson(legacyManifestPath)
    : null;
  assert.equal(
    legacyManifest?.schema,
    SNAPSHOT_SCHEMA,
    'Refusing to remove an unrecognized legacy output directory.',
  );
  fs.rmSync(legacyOutputRoot, { recursive: true });
}

const totalBytes =
  [...generated.values()].reduce((total, file) => total + file.bytes, 0) +
  manifestBytes.length;
console.log(
  JSON.stringify(
    {
      outputRoot: liveOutputRoot,
      generationId: EXPECTED_GENERATION,
      assets: summaries.length,
      observations: summaries.reduce(
        (total, asset) => total + (asset.dataSummary?.observations || 0),
        0,
      ),
      changes: changes.length,
      annotations: annotationCount,
      generatedFiles: generated.size + 1,
      generatedBytes: totalBytes,
      verifiedInputs: verifiedInputs.size,
    },
    null,
    2,
  ),
);
