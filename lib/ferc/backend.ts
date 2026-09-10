import type {
  ChangeItem,
  OperatingAssetDetail,
  OperatingAssetSummary,
  OperatingInstrumentDetail,
  OperatingInstrumentSummary,
} from './types';

export const FERC_GENERATION_ID =
  '0dccbd426f15372f1330737537f9aac58bc9547a2eaf81ef6f4b655f10cf2824';
export const FERC_CONTRACT_VERSION = '1.1.0';
const SNAPSHOT_ROOT = `/data/ferc/generations/${FERC_GENERATION_ID}`;

type GeneratedFile = {
  bytes: number;
  sha256: string;
  encoding: 'gzip' | 'identity';
  contentBytes: number;
  contentSha256: string;
};

export type FercSnapshotManifest = {
  schema: 'ferc_site_snapshot_v1';
  generationId: string;
  source: {
    contractSchema: string;
    contractVersion: string;
    asOf: string;
    registryVersion: string;
    publicationCodeVersion: string;
    publicationDatabaseIdentity: string;
  };
  counts: Record<string, number>;
  files: Record<string, GeneratedFile>;
};

export type FercDirectory = {
  schema: 'ferc_site_snapshot_v1';
  generationId: string;
  contractVersion: string;
  asOf: string;
  registryVersion: string;
  status: 'candidate_snapshot';
  notice: string;
  counts: Record<string, number>;
  coverage: Record<string, unknown>;
  coverageDefinitions: Record<string, string>;
  templateCoverage: {
    group: string;
    assets: number;
    entities: number;
    populated_pct: number;
    validated_pct: number;
  }[];
  largestUnmatchedCoreBuckets: Record<string, unknown>[];
  companies: Record<string, unknown>[];
  instruments: OperatingInstrumentSummary[];
  assets: OperatingAssetSummary[];
};

export type FercCatalog = {
  manifest: FercSnapshotManifest;
  directory: FercDirectory;
  assets: OperatingAssetSummary[];
};

const assetAliases: Record<string, string> = {
  transco: 'wmb-transco',
  tgp: 'kmi-tennessee-gas-pipeline',
  'elba-express': 'kmi-elba-express',
  transcolorado: 'kmi-transcolorado',
};

let catalogPromise: Promise<FercCatalog> | null = null;
let changesPromise: Promise<ChangeItem[]> | null = null;
const assetPromises = new Map<string, Promise<OperatingAssetDetail>>();
const instrumentPromises = new Map<
  string,
  Promise<OperatingInstrumentDetail>
>();
const lineagePromises = new Map<string, Promise<FercLineageShard>>();

export type FercLineageEdge = {
  input_order: string | number | null;
  input_role: string | null;
  operator_sign: string | null;
  coefficient: string | number | null;
  input_source_system: string | null;
  input_filing_id: string | null;
  input_source_fact_id: string | null;
  input_observation_id: string | null;
  input_context_id: string | null;
  input_concept: string | null;
  input_period: string | null;
  input_value: string | number | null;
  input_unit: string | null;
  input_version_status: string | null;
  input_population_id: string | null;
};

export type FercLineagePopulation = {
  population_id: string;
  source_system: string | null;
  source_table: string | null;
  filing_ids: string | string[] | null;
  inclusion_rule: string | null;
  exclusion_rule: string | null;
  row_count: string | number | null;
  candidate_count: string | number | null;
  excluded_count: string | number | null;
  member_digest: string | null;
  members_sample: string | unknown[] | null;
  aggregate_value: string | number | null;
  aggregate_unit: string | null;
  empty_reason: string | null;
  note: string | null;
};

export type FercLineageEntry = {
  edges: FercLineageEdge[];
  populations: FercLineagePopulation[];
};

type FercLineageShard = {
  schema: 'ferc_site_snapshot_v1_lineage';
  generationId: string;
  bucket: string;
  observations: Record<string, FercLineageEntry>;
};

function assertRecord(
  value: unknown,
  label: string,
): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} is not an object.`);
  }
}

function bytesToHex(bytes: ArrayBuffer) {
  return [...new Uint8Array(bytes)]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
}

async function digest(bytes: Uint8Array) {
  const input = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
  return bytesToHex(await crypto.subtle.digest('SHA-256', input));
}

async function decode(bytes: Uint8Array, encoding: GeneratedFile['encoding']) {
  if (encoding !== 'gzip') return bytes;
  if (!(bytes[0] === 0x1f && bytes[1] === 0x8b)) {
    return bytes;
  }
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('This browser cannot open the compressed FERC snapshot.');
  }
  const stream = new Blob([bytes as BlobPart])
    .stream()
    .pipeThrough(new DecompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function fetchGenerated<T>(
  relativePath: string,
  metadata: GeneratedFile,
): Promise<T> {
  const response = await fetch(
    `${SNAPSHOT_ROOT}/${relativePath}?sha256=${metadata.sha256}`,
    {
      cache: 'force-cache',
    },
  );
  if (!response.ok) {
    throw new Error(`FERC snapshot request failed (${response.status}).`);
  }
  const stored = new Uint8Array(await response.arrayBuffer());
  const browserDecodedGzip =
    metadata.encoding === 'gzip' && !(stored[0] === 0x1f && stored[1] === 0x8b);
  if (
    !browserDecodedGzip &&
    (stored.length !== metadata.bytes ||
      (await digest(stored)) !== metadata.sha256)
  ) {
    throw new Error(
      `FERC snapshot integrity check failed for ${relativePath}.`,
    );
  }
  const content = browserDecodedGzip
    ? stored
    : await decode(stored, metadata.encoding);
  if (
    content.length !== metadata.contentBytes ||
    (await digest(content)) !== metadata.contentSha256
  ) {
    throw new Error(`FERC snapshot content check failed for ${relativePath}.`);
  }
  return JSON.parse(new TextDecoder().decode(content)) as T;
}

async function loadManifest() {
  const response = await fetch(`${SNAPSHOT_ROOT}/manifest.json`, {
    cache: 'no-cache',
  });
  if (!response.ok)
    throw new Error('The FERC snapshot manifest is unavailable.');
  const value: unknown = await response.json();
  assertRecord(value, 'FERC snapshot manifest');
  if (
    value.schema !== 'ferc_site_snapshot_v1' ||
    value.generationId !== FERC_GENERATION_ID
  ) {
    throw new Error('The FERC snapshot generation is not the pinned release.');
  }
  const manifest = value as FercSnapshotManifest;
  if (manifest.source.contractVersion !== FERC_CONTRACT_VERSION) {
    throw new Error('The FERC backend contract version is unsupported.');
  }
  return manifest;
}

export function resolveAssetId(assetId: string) {
  return assetAliases[assetId] || assetId;
}

export function loadFercCatalog(): Promise<FercCatalog> {
  if (catalogPromise) return catalogPromise;
  const request = (async () => {
    const manifest = await loadManifest();
    const directoryFile = manifest.files['directory.json'];
    if (!directoryFile)
      throw new Error('The FERC snapshot is missing its directory route.');
    const directory = await fetchGenerated<FercDirectory>(
      'directory.json',
      directoryFile,
    );
    if (
      directory.generationId !== FERC_GENERATION_ID ||
      !Array.isArray(directory.assets)
    ) {
      throw new Error('The FERC catalog failed runtime validation.');
    }
    const uniqueIds = new Set(directory.assets.map((asset) => asset.id));
    if (uniqueIds.size !== directory.assets.length) {
      throw new Error('The FERC asset directory contains duplicate IDs.');
    }
    return {
      manifest,
      directory,
      assets: directory.assets,
    };
  })();
  catalogPromise = request;
  request.catch(() => {
    if (catalogPromise === request) catalogPromise = null;
  });
  return request;
}

export function loadFercChanges(): Promise<ChangeItem[]> {
  if (changesPromise) return changesPromise;
  const request = loadFercCatalog().then(async (catalog) => {
    const metadata = catalog.manifest.files['changes.json.gz'];
    if (!metadata)
      throw new Error('The FERC snapshot is missing its change archive.');
    const payload = await fetchGenerated<{
      schema: string;
      generationId: string;
      changes: ChangeItem[];
    }>('changes.json.gz', metadata);
    if (
      payload.schema !== 'ferc_site_snapshot_v1' ||
      payload.generationId !== FERC_GENERATION_ID ||
      !Array.isArray(payload.changes)
    ) {
      throw new Error('The FERC change archive failed runtime validation.');
    }
    return payload.changes;
  });
  changesPromise = request;
  request.catch(() => {
    if (changesPromise === request) changesPromise = null;
  });
  return request;
}

export async function loadFercAsset(assetId: string) {
  const resolvedId = resolveAssetId(assetId);
  const catalog = await loadFercCatalog();
  const asset = catalog.assets.find((candidate) => candidate.id === resolvedId);
  if (!asset) throw new Error(`Unknown FERC asset: ${assetId}`);
  const expectedPath = `${SNAPSHOT_ROOT}/assets/${resolvedId}.json.gz`;
  if (asset.detailPath !== expectedPath) {
    throw new Error('The FERC asset route is outside the catalog whitelist.');
  }
  let request = assetPromises.get(resolvedId);
  if (!request) {
    request = (async () => {
      const relativePath = `assets/${resolvedId}.json.gz`;
      const metadata = catalog.manifest.files[relativePath];
      if (!metadata)
        throw new Error('The FERC asset payload is outside the manifest.');
      const detail = await fetchGenerated<OperatingAssetDetail>(
        relativePath,
        metadata,
      );
      if (
        detail.schema !== 'ferc_site_snapshot_v1' ||
        detail.generationId !== FERC_GENERATION_ID ||
        detail.asset.id !== resolvedId ||
        !Array.isArray(detail.metrics) ||
        !Array.isArray(detail.events)
      ) {
        throw new Error('The FERC asset payload failed runtime validation.');
      }
      return detail;
    })();
    assetPromises.set(resolvedId, request);
    request.catch(() => {
      if (assetPromises.get(resolvedId) === request)
        assetPromises.delete(resolvedId);
    });
  }
  return request;
}

export async function loadFercInstrument(instrumentId: string) {
  const catalog = await loadFercCatalog();
  const instrument = catalog.directory.instruments.find(
    (candidate) => candidate.id === instrumentId,
  );
  if (!instrument) throw new Error(`Unknown FERC instrument: ${instrumentId}`);
  const expectedPath = `${SNAPSHOT_ROOT}/instruments/${instrumentId}.json.gz`;
  if (instrument.detailPath !== expectedPath) {
    throw new Error(
      'The FERC instrument route is outside the catalog whitelist.',
    );
  }
  let request = instrumentPromises.get(instrumentId);
  if (!request) {
    request = (async () => {
      const relativePath = `instruments/${instrumentId}.json.gz`;
      const metadata = catalog.manifest.files[relativePath];
      if (!metadata) {
        throw new Error('The FERC instrument payload is outside the manifest.');
      }
      const detail = await fetchGenerated<OperatingInstrumentDetail>(
        relativePath,
        metadata,
      );
      if (
        detail.schema !== 'ferc_site_snapshot_v1' ||
        detail.generationId !== FERC_GENERATION_ID ||
        detail.instrument.id !== instrumentId ||
        !Array.isArray(detail.metrics)
      ) {
        throw new Error(
          'The FERC instrument payload failed runtime validation.',
        );
      }
      return detail;
    })();
    instrumentPromises.set(instrumentId, request);
    request.catch(() => {
      if (instrumentPromises.get(instrumentId) === request) {
        instrumentPromises.delete(instrumentId);
      }
    });
  }
  return request;
}

export async function loadFercLineage(
  relativeUrl: string,
  observationId: string,
): Promise<FercLineageEntry> {
  const expectedBucket = observationId.slice(4, 6);
  const expectedUrl = `${SNAPSHOT_ROOT}/provenance/lineage-${expectedBucket}.json.gz`;
  if (
    !/^obs-[a-f0-9]{32}$/.test(observationId) ||
    relativeUrl !== expectedUrl
  ) {
    throw new Error('The lineage request is outside the snapshot whitelist.');
  }
  const catalog = await loadFercCatalog();
  const relativePath = `provenance/lineage-${expectedBucket}.json.gz`;
  const metadata = catalog.manifest.files[relativePath];
  if (!metadata)
    throw new Error('The complete lineage shard is not in the manifest.');
  let request = lineagePromises.get(relativePath);
  if (!request) {
    request = fetchGenerated<FercLineageShard>(relativePath, metadata).then(
      (shard) => {
        if (
          shard.schema !== 'ferc_site_snapshot_v1_lineage' ||
          shard.generationId !== FERC_GENERATION_ID ||
          shard.bucket !== expectedBucket ||
          !shard.observations
        ) {
          throw new Error(
            'The complete lineage shard failed runtime validation.',
          );
        }
        return shard;
      },
    );
    lineagePromises.set(relativePath, request);
    request.catch(() => {
      if (lineagePromises.get(relativePath) === request)
        lineagePromises.delete(relativePath);
    });
  }
  const shard = await request;
  const entry = shard.observations[observationId];
  if (
    !entry ||
    !Array.isArray(entry.edges) ||
    !Array.isArray(entry.populations)
  ) {
    throw new Error(
      'The selected observation is absent from its lineage shard.',
    );
  }
  return entry;
}
