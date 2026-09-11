'use client';

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Building2,
  CalendarDays,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  FolderKanban,
  History,
  Info,
  GitCompareArrows,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  X,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { DotItemDotProps } from 'recharts';
import { ChartContainer } from '@/components/ui/chart';
import { BackendAssetComparison } from '@/components/ferc/backend-asset-comparison';
import {
  BackendAssetDetail,
  formatObservationValue,
  observationSource,
  safeFercUrl,
} from '@/components/ferc/backend-asset-detail';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import {
  changes as fixtureChanges,
  glossary,
  projectSource,
  projects,
  sourceProvenance,
} from '@/lib/ferc/data';
import {
  loadFercAsset,
  loadFercCatalog,
  loadFercChanges,
  loadFercInstrument,
  loadFercLineage,
  reloadForFercSnapshotUpdate,
  resolveAssetId,
  type FercCatalog,
  type FercLineageEntry,
} from '@/lib/ferc/backend';
import {
  MAX_COMPARISON_ASSETS,
  isComparisonEligible,
  validateComparisonSelection,
} from '@/lib/ferc/comparison';
import {
  formatBackendValue,
  formatCompact,
  formatDate,
  humanizeFercText,
  isMetricPresentationBlocked,
  margin,
  percentChange,
  presentationUnit,
} from '@/lib/ferc/format';
import type {
  AnnualPoint,
  ChangeItem,
  OperatingAssetDetail,
  OperatingAssetSummary,
  OperatingAssetView,
  OperatingInstrumentDetail,
  OperatingInstrumentSummary,
  ProjectView,
  QuarterPoint,
  SourceDetail,
} from '@/lib/ferc/types';

type ViewName = 'changes' | 'assets' | 'projects';
type RouteState = {
  view: ViewName;
  asset?: string;
  project?: string;
  company: string;
  compareIds: string[];
};

const sourceForPoint = (asset: OperatingAssetView, point: QuarterPoint) =>
  asset.sources.find((source) => source.id === point.sourceId);
const projectCompanies: string[] = [
  ...new Set(projects.map((project) => project.company)),
].sort();
const projectStages: string[] = [
  ...new Set(projects.map((project) => project.current_stage)),
].sort();

const followInternalLink = (
  event: React.MouseEvent<HTMLAnchorElement>,
  navigate: () => void,
) => {
  if (
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  ) {
    event.preventDefault();
    navigate();
  }
};

const routeHref = (view: ViewName, company: string, id?: string) => {
  const params = new URLSearchParams({ view });
  if (company !== 'All companies') params.set('company', company);
  if (view === 'assets' && id) params.set('asset', id);
  if (view === 'projects' && id) params.set('project', id);
  return `?${params.toString()}`;
};

function readRoute(): RouteState {
  if (typeof window === 'undefined')
    return { view: 'changes', company: 'All companies', compareIds: [] };
  const params = new URLSearchParams(window.location.search);
  const raw = params.get('view');
  const view: ViewName =
    raw === 'assets' || raw === 'projects' ? raw : 'changes';
  const requestedCompany =
    params.get('company') ??
    params.get('assetCompany') ??
    params.get('projectCompany') ??
    params.get('changeCompany');
  return {
    view,
    asset: params.get('asset') ?? undefined,
    project: params.get('project') ?? undefined,
    company: requestedCompany || 'All companies',
    compareIds: (params.get('compare') ?? '')
      .split(',')
      .map((id) => id.trim())
      .filter(Boolean),
  };
}

function InfoTerm({
  term,
  children,
}: {
  term: string;
  children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();
  const pointerDown = useRef(false);
  return (
    <span className="info-term">
      {children ?? term}
      <span className="term-control">
        <button
          type="button"
          aria-label={`Explain ${term}`}
          aria-expanded={open}
          aria-describedby={open ? tooltipId : undefined}
          onPointerDown={() => {
            pointerDown.current = true;
          }}
          onFocus={() => {
            if (!pointerDown.current) setOpen(true);
          }}
          onClick={() => {
            pointerDown.current = true;
            setOpen((current) => !current);
            window.setTimeout(() => {
              pointerDown.current = false;
            }, 0);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setOpen(false);
              event.currentTarget.blur();
            }
          }}
          onBlur={() => {
            pointerDown.current = false;
            setOpen(false);
          }}
        >
          <Info />
        </button>
        {open && (
          <span id={tooltipId} role="tooltip">
            <strong>{term}</strong>
            {glossary[term] ??
              'Definition unavailable in this development snapshot.'}
          </span>
        )}
      </span>
    </span>
  );
}

function Status({
  tone = 'neutral',
  children,
}: {
  tone?: 'good' | 'warn' | 'neutral' | 'project';
  children: React.ReactNode;
}) {
  return <span className={`status status-${tone}`}>{children}</span>;
}

/* oxlint-disable jsx-a11y/prefer-tag-over-role -- SVG chart marks cannot contain an HTML button. */
function SourceDot({
  cx,
  cy,
  payload,
  color,
  label,
  onSelect,
}: DotItemDotProps & {
  color: string;
  label: (payload: Record<string, unknown>) => string;
  onSelect: (payload: Record<string, unknown>) => void;
}) {
  if (typeof cx !== 'number' || typeof cy !== 'number' || !payload) return null;
  const point = payload as Record<string, unknown>;
  const select = () => onSelect(point);
  return (
    <circle
      cx={cx}
      cy={cy}
      r={4.5}
      fill="white"
      stroke={color}
      strokeWidth={2.25}
      role="button"
      tabIndex={0}
      focusable="true"
      aria-label={label(point)}
      className="source-chart-dot"
      onClick={select}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          select();
        }
      }}
    />
  );
}
/* oxlint-enable jsx-a11y/prefer-tag-over-role */

function FullLineage({
  path,
  observationId,
}: {
  path: string;
  observationId: string;
}) {
  const [entry, setEntry] = useState<FercLineageEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(50);
  useEffect(() => {
    let cancelled = false;
    loadFercLineage(path, observationId)
      .then((value) => {
        if (!cancelled) setEntry(value);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          if (reloadForFercSnapshotUpdate(reason)) return;
          setError(
            reason instanceof Error
              ? reason.message
              : 'Complete lineage is unavailable.',
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [observationId, path]);
  if (error) return <p className="lineage-error">{error}</p>;
  if (!entry)
    return (
      <p className="lineage-loading">Loading the verified complete lineage…</p>
    );
  return (
    <div className="full-lineage">
      <p>
        {entry.edges.length.toLocaleString()} derivation edges ·{' '}
        {entry.populations.length.toLocaleString()} population descriptors
      </p>
      {entry.edges.length > 0 && (
        <div className="lineage-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Role</th>
                <th>Operation</th>
                <th>Input identity</th>
                <th>Concept</th>
                <th>Value</th>
                <th>Period</th>
              </tr>
            </thead>
            <tbody>
              {entry.edges.slice(0, visible).map((edge, index) => (
                <tr
                  key={`${edge.input_order}-${edge.input_source_fact_id}-${index}`}
                >
                  <td>{edge.input_role || 'Input'}</td>
                  <td>
                    {[edge.operator_sign, edge.coefficient]
                      .filter(
                        (value) =>
                          value !== null && value !== undefined && value !== '',
                      )
                      .join(' ') || '—'}
                  </td>
                  <td className="lineage-identities">
                    {[
                      edge.input_source_system
                        ? `System: ${edge.input_source_system}`
                        : null,
                      edge.input_filing_id
                        ? `Filing: ${edge.input_filing_id}`
                        : null,
                      edge.input_source_fact_id
                        ? `Fact: ${edge.input_source_fact_id}`
                        : null,
                      edge.input_observation_id
                        ? `Observation: ${edge.input_observation_id}`
                        : null,
                      edge.input_context_id
                        ? `Context: ${edge.input_context_id}`
                        : null,
                      edge.input_population_id
                        ? `Population: ${edge.input_population_id}`
                        : null,
                      edge.input_version_status
                        ? `Version: ${edge.input_version_status}`
                        : null,
                    ]
                      .filter((value): value is string => Boolean(value))
                      .map((value) => (
                        <code key={value}>{value}</code>
                      ))}
                  </td>
                  <td>{edge.input_concept || 'Referenced row'}</td>
                  <td>
                    {edge.input_value !== null && edge.input_value !== ''
                      ? formatBackendValue(
                          edge.input_value,
                          edge.input_unit,
                          true,
                        )
                      : '—'}
                  </td>
                  <td>{edge.input_period || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {visible < entry.edges.length && (
        <button
          className="secondary-button"
          onClick={() => setVisible(entry.edges.length)}
        >
          Show all {entry.edges.length.toLocaleString()} edges
        </button>
      )}
      {entry.populations.map((population) => (
        <details key={population.population_id} className="lineage-population">
          <summary>Population {population.population_id}</summary>
          <p>
            {[population.source_system, population.source_table]
              .filter(Boolean)
              .join(' · ') || 'Source not supplied'}
          </p>
          <p>{population.inclusion_rule || 'No inclusion rule supplied.'}</p>
          <p>{population.exclusion_rule || 'No exclusion rule supplied.'}</p>
          <small>
            {population.row_count ?? '0'} included ·{' '}
            {population.candidate_count ?? 'not supplied'} candidates ·{' '}
            {population.excluded_count ?? '0'} excluded
          </small>
          {[population.aggregate_value, population.aggregate_unit].some(
            (value) => value !== null && value !== undefined && value !== '',
          ) && (
            <p>
              Aggregate:{' '}
              {formatBackendValue(
                population.aggregate_value,
                population.aggregate_unit,
                true,
              )}
            </p>
          )}
          {population.filing_ids && (
            <p>
              Filings:{' '}
              {Array.isArray(population.filing_ids)
                ? population.filing_ids.join(', ')
                : population.filing_ids}
            </p>
          )}
          <code>{population.member_digest || 'Digest not supplied'}</code>
          {population.empty_reason && <p>{population.empty_reason}</p>}
          {population.note && <p>{population.note}</p>}
        </details>
      ))}
    </div>
  );
}

function SourceDrawer({
  source,
  onClose,
}: {
  source: SourceDetail | null;
  onClose: () => void;
}) {
  const sourceUrl = safeFercUrl(source?.url);
  const rows = source
    ? [
        ['Availability', source.availability],
        ['Origin', source.origin],
        ['Method', source.method],
        [
          'Version',
          source.filingVersion
            ? humanizeFercText(source.filingVersion)
            : 'Not supplied',
        ],
        ['Canonical occurrence', source.canonicalStatus || 'Not supplied'],
        [
          'Canonical reason',
          source.canonicalReason
            ? humanizeFercText(source.canonicalReason)
            : 'Not supplied',
        ],
        [
          'Acceptance status',
          source.acceptanceStatus
            ? humanizeFercText(source.acceptanceStatus)
            : 'Not supplied',
        ],
        [
          'Data origin',
          source.dataOrigin
            ? humanizeFercText(source.dataOrigin)
            : 'Not supplied',
        ],
        ['Validation', source.validation],
        ['Entity / facility scope', source.scope || 'Not supplied'],
        ['Period / date', source.period || 'Not supplied'],
        ['Display unit', source.displayUnit || source.unit || 'Not supplied'],
        ['Filed / stored unit', source.filedUnit || 'Not supplied'],
        [
          'Display scale',
          source.displayScale === null || source.displayScale === undefined
            ? 'Not supplied'
            : source.displayScale === 1
              ? '×1 (no scaling)'
              : `×${source.displayScale.toLocaleString('en-US')}`,
        ],
        ['Native identity', source.nativeIdentity || 'Not supplied'],
        ['Docket', source.docket || 'Not applicable'],
        ['Accession', source.accession || 'Not supplied'],
        ['Filing occurrence', source.filingId || 'Not supplied'],
        ['Source fact', source.sourceFactId || 'Not supplied'],
        ['Document ID', source.documentId || 'Not supplied'],
        [
          'Reporting date',
          source.reportingDate
            ? formatDate(source.reportingDate)
            : 'Not supplied',
        ],
        [
          'Filed date',
          source.filedDate ? formatDate(source.filedDate) : 'Not supplied',
        ],
        [
          'Posted date',
          source.postedDate ? formatDate(source.postedDate) : 'Not supplied',
        ],
        [
          'Issued date',
          source.issuedDate ? formatDate(source.issuedDate) : 'Not supplied',
        ],
        [
          'Regulatory effective date',
          source.effectiveDate
            ? formatDate(source.effectiveDate)
            : 'Not supplied',
        ],
        [
          'Retrieved',
          source.retrievedDate
            ? formatDate(source.retrievedDate)
            : 'Not supplied',
        ],
        [
          'First seen',
          source.firstSeen ? formatDate(source.firstSeen) : 'Not supplied',
        ],
      ]
    : [];
  return (
    <Sheet open={Boolean(source)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        className="source-sheet sm:max-w-[500px]"
        showCloseButton={false}
      >
        {source && (
          <>
            <SheetHeader className="source-sheet-head">
              <button
                type="button"
                className="source-close-button"
                aria-label="Close"
                onClick={onClose}
              >
                <X aria-hidden="true" />
              </button>
              <p className="eyebrow">Source & details</p>
              <SheetTitle>{source.title}</SheetTitle>
              <SheetDescription>{source.sourceSystem}</SheetDescription>
            </SheetHeader>
            <div className="source-sheet-body">
              {source.description && (
                <p className="source-description">{source.description}</p>
              )}
              {source.value && (
                <div className="source-value">
                  <span>{source.valueLabel ?? 'Exact source value'}</span>
                  <strong>{source.value}</strong>
                </div>
              )}
              {source.storedValue && (
                <div className="source-value source-filed-value">
                  <span>Stored interpretation</span>
                  <strong>{source.storedValue}</strong>
                </div>
              )}
              {source.filedValue && (
                <div className="source-value source-filed-value">
                  <span>Filed source value</span>
                  <strong>{source.filedValue}</strong>
                </div>
              )}
              {source.filedValueRaw && (
                <details className="raw-filed-value">
                  <summary>View filed source record</summary>
                  <pre>{source.filedValueRaw}</pre>
                </details>
              )}
              {source.comparisonValue && (
                <div className="source-value source-comparison-value">
                  <span>
                    {source.comparisonValueLabel ?? 'Comparison base value'}
                  </span>
                  <strong>{source.comparisonValue}</strong>
                </div>
              )}
              <dl className="source-grid">
                {rows.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              {source.formula && (
                <section className="drawer-section">
                  <h3>Formula</h3>
                  <code>{source.formula}</code>
                </section>
              )}
              {source.inputs?.length ? (
                <section className="drawer-section">
                  <h3>Inputs and references</h3>
                  <dl className="source-inputs">
                    {source.inputs.map((input, index) => (
                      <div
                        key={`${input.label}-${input.period ?? ''}-${input.sourceId ?? index}`}
                      >
                        <dt>{input.label}</dt>
                        <dd>
                          <strong>{input.value}</strong>
                          {input.period && <span>{input.period}</span>}
                          {input.sourceId && <code>{input.sourceId}</code>}
                          {input.details?.map((detail) => (
                            <small key={detail}>{detail}</small>
                          ))}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </section>
              ) : null}
              {source.populations?.length ? (
                <section className="drawer-section">
                  <h3>Population lineage</h3>
                  <div className="source-populations">
                    {source.populations.map((population) => (
                      <details key={population.id} open>
                        <summary>{population.id}</summary>
                        {population.source && <p>{population.source}</p>}
                        <dl>
                          <div>
                            <dt>Rows</dt>
                            <dd>
                              {population.rowCount?.toLocaleString() ??
                                'Not supplied'}
                            </dd>
                          </div>
                          <div>
                            <dt>Candidates</dt>
                            <dd>
                              {population.candidateCount?.toLocaleString() ??
                                'Not supplied'}
                            </dd>
                          </div>
                          <div>
                            <dt>Excluded</dt>
                            <dd>
                              {population.excludedCount?.toLocaleString() ??
                                'Not supplied'}
                            </dd>
                          </div>
                        </dl>
                        {population.aggregate && (
                          <p>Aggregate: {population.aggregate}</p>
                        )}
                        {population.inclusionRule && (
                          <p>Included when: {population.inclusionRule}</p>
                        )}
                        {population.exclusionRule && (
                          <p>Excluded when: {population.exclusionRule}</p>
                        )}
                        {population.filingIds?.length ? (
                          <p>Filings: {population.filingIds.join(', ')}</p>
                        ) : null}
                        {population.memberDigest && (
                          <code>{population.memberDigest}</code>
                        )}
                        {population.note && <p>{population.note}</p>}
                      </details>
                    ))}
                  </div>
                </section>
              ) : null}
              {source.lineagePath && source.lineageObservationId && (
                <section className="drawer-section">
                  <h3>Complete derivation lineage</h3>
                  <FullLineage
                    key={source.lineageObservationId}
                    path={source.lineagePath}
                    observationId={source.lineageObservationId}
                  />
                </section>
              )}
              <section className="drawer-section">
                <h3>Warnings and limits</h3>
                {source.warnings?.length ? (
                  <ul>
                    {[...new Set(source.warnings)].map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No warnings were supplied for this selected record.</p>
                )}
              </section>
              {sourceUrl ? (
                <a
                  className="primary-link"
                  href={sourceUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open public source <ArrowUpRight />
                </a>
              ) : (
                <div className="unavailable-inline">
                  <CircleAlert /> No usable allowlisted public source URL is
                  available for this record.
                  {source.nativeIdentity
                    ? ' Its source identity is retained above.'
                    : ' No native source identity was supplied.'}
                </div>
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function GlossaryDrawer({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Sheet open={open} onOpenChange={(next) => !next && onClose()}>
      <SheetContent className="source-sheet sm:max-w-[500px]">
        <SheetHeader className="source-sheet-head">
          <p className="eyebrow">Reference</p>
          <SheetTitle>FERC terminology</SheetTitle>
          <SheetDescription>
            Plain-language definitions and the limitations that matter.
          </SheetDescription>
        </SheetHeader>
        <div className="source-sheet-body glossary-list">
          {Object.entries(glossary).map(([term, definition]) => (
            <div key={term}>
              <h3>{term}</h3>
              <p>{definition}</p>
            </div>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function AppHeader({
  route,
  go,
  setCompany,
  openGlossary,
  workspaceCompanies,
  catalog,
}: {
  route: RouteState;
  go: (view: ViewName, id?: string) => void;
  setCompany: (company: string) => void;
  openGlossary: () => void;
  workspaceCompanies: string[];
  catalog: FercCatalog | null;
}) {
  const nav = [
    { view: 'assets' as const, label: 'Assets', icon: Building2 },
    { view: 'projects' as const, label: 'Projects', icon: FolderKanban },
    { view: 'changes' as const, label: 'Changes', icon: History },
  ];
  return (
    <>
      <header className="topbar">
        <Link
          className="wordmark"
          href={routeHref('changes', route.company)}
          prefetch={false}
          onClick={(event) => followInternalLink(event, () => go('changes'))}
          aria-label="FERC Intelligence home"
        >
          <span className="wordmark-mark">F</span>
          <span>FERC Intelligence</span>
        </Link>
        <label className="workspace-company">
          <span>Company workspace</span>
          <select
            aria-label="Company workspace"
            value={route.company}
            onChange={(event) => setCompany(event.target.value)}
          >
            <option>All companies</option>
            {workspaceCompanies.map((company) => (
              <option key={company} value={company}>
                {company === 'Company not mapped'
                  ? 'Unmapped project companies'
                  : company}
              </option>
            ))}
          </select>
        </label>
        <nav aria-label="Primary navigation">
          {nav.map((item) => (
            <Link
              key={item.view}
              href={routeHref(item.view, route.company)}
              prefetch={false}
              aria-current={route.view === item.view ? 'page' : undefined}
              className={
                route.view === item.view ? 'nav-link active' : 'nav-link'
              }
              onClick={(event) =>
                followInternalLink(event, () => go(item.view))
              }
            >
              <item.icon aria-hidden="true" />
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="header-actions">
          <button className="glossary-button" onClick={openGlossary}>
            <BookOpen /> Glossary
          </button>
          <span className="snapshot-pill">
            <i /> Candidate snapshot
          </span>
        </div>
      </header>
      <div className="snapshot-strip">
        <span>Data snapshot</span>
        {catalog
          ? `As of ${formatDate(catalog.directory.asOf)}`
          : 'Loading verified data…'}
        {' · '}Updates are not live
      </div>
    </>
  );
}

function FilterSummary({
  items,
  clear,
}: {
  items: string[];
  clear: () => void;
}) {
  if (!items.length) return null;
  return (
    <div className="active-filters">
      <span>Active filters</span>
      {items.map((item) => (
        <em key={item}>{item}</em>
      ))}
      <button onClick={clear}>
        Clear filters <X />
      </button>
    </div>
  );
}

function EmptyState({
  filtered = false,
  title,
  description,
}: {
  filtered?: boolean;
  title?: string;
  description?: string;
}) {
  return (
    <div className="empty-state">
      <Search />
      <h2>{title ?? (filtered ? 'No filter matches' : 'No recent changes')}</h2>
      <p>
        {description ??
          (filtered
            ? 'Clear one or more filters to return to the snapshot results.'
            : 'No supplied records fall in this view and date range. Nothing has been inferred.')}
      </p>
    </div>
  );
}

function ChangesView({
  openSource,
  companyContext,
  changes,
  changeRegimes,
}: {
  openSource: (source: SourceDetail) => void;
  companyContext: string;
  changes: ChangeItem[];
  changeRegimes: string[];
}) {
  const [tab, setTab] = useState<'substantive' | 'archive' | 'review'>(
    'substantive',
  );
  const [query, setQuery] = useState('');
  const [domain, setDomain] = useState('All sections');
  const [category, setCategory] = useState('All categories');
  const [regime, setRegime] = useState('All regimes');
  const [dateRange, setDateRange] = useState('Snapshot window');
  const [more, setMore] = useState(false);
  const [visibleLimit, setVisibleLimit] = useState(50);
  const [reviewMarker, setReviewMarker] = useState<string | null>(null);
  const [markerPersistence, setMarkerPersistence] = useState(true);
  const [filtersHydrated, setFiltersHydrated] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const initial = new URLSearchParams(window.location.search);
      const feedParam = initial.get('changeFeed');
      const initialTab =
        feedParam === 'archive' || feedParam === 'review'
          ? feedParam
          : 'substantive';
      let marker: string | null = null;
      try {
        const stored = localStorage.getItem('ferc-review-marker');
        marker = stored && Number.isFinite(Date.parse(stored)) ? stored : null;
      } catch {
        setMarkerPersistence(false);
      }
      const domainParam = initial.get('changeDomain');
      const categoryParam = initial.get('changeCategory');
      const regimeParam = initial.get('changeRegime');
      const dateParam = initial.get('changeDate');
      const validCategories = changes
        .filter((item) => item.kind === initialTab)
        .map((item) => item.category);
      setTab(initialTab);
      setQuery(initial.get('changeQ') ?? '');
      setDomain(
        domainParam === 'Operating Assets' || domainParam === 'Projects'
          ? domainParam
          : 'All sections',
      );
      setCategory(
        categoryParam && validCategories.includes(categoryParam)
          ? categoryParam
          : 'All categories',
      );
      setRegime(
        regimeParam && changeRegimes.includes(regimeParam)
          ? regimeParam
          : 'All regimes',
      );
      setDateRange(
        dateParam === '2026 only' ||
          (dateParam === 'Since last review' && marker)
          ? dateParam
          : 'Snapshot window',
      );
      setMore(
        Boolean(
          (categoryParam && validCategories.includes(categoryParam)) ||
          (regimeParam && changeRegimes.includes(regimeParam)),
        ),
      );
      setReviewMarker(marker);
      setFiltersHydrated(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [changeRegimes, changes]);
  useEffect(() => {
    if (!filtersHydrated) return;
    const url = new URL(window.location.href);
    const fields = {
      changeFeed: tab,
      changeQ: query,
      changeDomain: domain,
      changeCategory: category,
      changeRegime: regime,
      changeDate: dateRange,
    };
    Object.entries(fields).forEach(([key, value]) =>
      value &&
      !value.startsWith('All ') &&
      value !== 'Snapshot window' &&
      value !== 'substantive'
        ? url.searchParams.set(key, value)
        : url.searchParams.delete(key),
    );
    window.history.replaceState(window.history.state, '', url);
  }, [tab, query, domain, category, regime, dateRange, filtersHydrated]);

  const filtered = useMemo(
    () =>
      changes
        .filter((item) => {
          if (item.kind !== tab) return false;
          const haystack =
            `${item.title} ${item.entity} ${item.company} ${item.category}`.toLowerCase();
          if (query && !haystack.includes(query.toLowerCase())) return false;
          if (
            companyContext !== 'All companies' &&
            item.company !== companyContext &&
            !item.companies?.includes(companyContext)
          )
            return false;
          if (domain !== 'All sections' && item.domain !== domain) return false;
          if (category !== 'All categories' && item.category !== category)
            return false;
          if (regime !== 'All regimes' && item.regime !== regime) return false;
          if (dateRange === '2026 only' && !item.date.startsWith('2026'))
            return false;
          if (dateRange === 'Since last review') {
            const firstSeen = item.firstSeen
              ? Date.parse(item.firstSeen)
              : Number.NaN;
            const marker = reviewMarker ? Date.parse(reviewMarker) : Number.NaN;
            if (
              !Number.isFinite(firstSeen) ||
              !Number.isFinite(marker) ||
              firstSeen <= marker
            )
              return false;
          }
          return true;
        })
        .sort((left, right) => right.date.localeCompare(left.date)),
    [
      tab,
      query,
      companyContext,
      domain,
      category,
      regime,
      dateRange,
      reviewMarker,
      changes,
    ],
  );

  const active = [
    query && `Search: ${query}`,
    domain !== 'All sections' && domain,
    category !== 'All categories' && category,
    regime !== 'All regimes' && regime,
    dateRange !== 'Snapshot window' && dateRange,
  ].filter(Boolean) as string[];
  const clear = () => {
    setQuery('');
    setDomain('All sections');
    setCategory('All categories');
    setRegime('All regimes');
    setDateRange('Snapshot window');
  };
  const markReview = () => {
    const value = new Date().toISOString();
    try {
      localStorage.setItem('ferc-review-marker', value);
    } catch {
      setMarkerPersistence(false);
    }
    setReviewMarker(value);
  };
  const categories = [
    ...new Set(
      changes.filter((item) => item.kind === tab).map((item) => item.category),
    ),
  ];
  const displayed = filtered.slice(0, visibleLimit);

  return (
    <main className="page-shell">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Weekly review</p>
          <h1>Changes</h1>
          <p>
            Substantive developments, filing archive and data-review issues
            remain distinct.
          </p>
        </div>
        <button className="review-button" onClick={markReview}>
          {reviewMarker ? (
            <>
              <Check /> Reviewed {formatDate(reviewMarker)}
            </>
          ) : (
            'Set review marker'
          )}
        </button>
      </div>
      {!markerPersistence && (
        <output className="chart-record-note">
          Browser storage is unavailable. Your review marker lasts only while
          this view is open; it does not resolve backend review records.
        </output>
      )}
      <nav className="feed-tabs" aria-label="Change feed type">
        {[
          ['substantive', 'Substantive changes'],
          ['archive', 'Filing archive'],
          ['review', 'Data review'],
        ].map(([value, label]) => (
          <button
            key={value}
            aria-pressed={tab === value}
            onClick={() => {
              setTab(value as typeof tab);
              setCategory('All categories');
            }}
          >
            {label}
            <span>{changes.filter((item) => item.kind === value).length}</span>
          </button>
        ))}
      </nav>
      <section className="filter-bar" aria-label="Change filters">
        <label className="search-field">
          <span>Search</span>
          <div className="input-with-icon">
            <Search />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Company, asset, project or docket"
            />
          </div>
        </label>
        <label>
          <span>Section</span>
          <select
            value={domain}
            onChange={(event) => setDomain(event.target.value)}
          >
            <option>All sections</option>
            <option>Operating Assets</option>
            <option>Projects</option>
          </select>
        </label>
        <label>
          <span>Date range</span>
          <select
            value={dateRange}
            onChange={(event) => setDateRange(event.target.value)}
          >
            <option>Snapshot window</option>
            <option>2026 only</option>
            <option disabled={!reviewMarker}>Since last review</option>
          </select>
        </label>
        <button
          className="more-filter-button"
          onClick={() => setMore(!more)}
          aria-expanded={more}
        >
          <SlidersHorizontal /> More filters <ChevronDown />
        </button>
        {more && (
          <>
            <label className="category-filter">
              <span>Event category</span>
              <select
                value={category}
                onChange={(event) => setCategory(event.target.value)}
              >
                <option>All categories</option>
                {categories.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label className="category-filter">
              <span>Asset regime</span>
              <select
                value={regime}
                onChange={(event) => setRegime(event.target.value)}
              >
                <option>All regimes</option>
                {changeRegimes.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
          </>
        )}
      </section>
      <FilterSummary items={active} clear={clear} />
      <div className="results-meta">
        <strong>
          {filtered.length}{' '}
          {tab === 'archive'
            ? 'filings'
            : tab === 'review'
              ? 'review issues'
              : 'substantive changes'}
        </strong>
        <span>
          {dateRange === 'Since last review'
            ? 'Filtered by supplied first-seen timestamp; records without it are excluded'
            : 'Sorted by relevant event date, newest first · Snapshot window includes available history'}
        </span>
      </div>
      <div className="change-list">
        {filtered.length ? (
          displayed.map((item) => (
            <ChangeCard key={item.id} item={item} openSource={openSource} />
          ))
        ) : (
          <EmptyState
            filtered={active.length > 0}
            title={
              dateRange === 'Since last review'
                ? 'No changes since last review'
                : undefined
            }
            description={
              dateRange === 'Since last review'
                ? `No matching copied record has a supplied first-seen timestamp after ${formatDate(reviewMarker ?? undefined)}. Records without one are excluded.`
                : undefined
            }
          />
        )}
      </div>
      {displayed.length < filtered.length && (
        <div className="load-more-row">
          <button
            className="secondary-button"
            onClick={() => setVisibleLimit((value) => value + 50)}
          >
            Show 50 more · {filtered.length - displayed.length} remaining
          </button>
        </div>
      )}
    </main>
  );
}

function ChangeCard({
  item,
  openSource,
}: {
  item: ChangeItem;
  openSource: (source: SourceDetail) => void;
}) {
  const date = new Date(item.date);
  const day = String(date.getUTCDate()).padStart(2, '0');
  const month = date
    .toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' })
    .toUpperCase();
  return (
    <article className="change-card">
      <div className="change-date">
        <strong>{day}</strong>
        <span>
          {month}
          <br />
          {date.getUTCFullYear()}
        </span>
      </div>
      <div className="change-body">
        <div className="card-kicker">
          <Status tone={item.domain === 'Projects' ? 'project' : 'neutral'}>
            {item.domain === 'Projects' ? 'Project' : 'Operating'}
          </Status>
          {item.category}
        </div>
        <h2>{item.title}</h2>
        <p>{item.explanation}</p>
        <dl className="date-grid">
          <div>
            <dt>Relevant date</dt>
            <dd>{formatDate(item.date)}</dd>
          </div>
          {item.filedDate && (
            <div>
              <dt>Filed date</dt>
              <dd>{formatDate(item.filedDate)}</dd>
            </div>
          )}
          {item.postedDate && (
            <div>
              <dt>Posted date</dt>
              <dd>{formatDate(item.postedDate)}</dd>
            </div>
          )}
          {item.reportingPeriod && (
            <div>
              <dt>Reporting period</dt>
              <dd>{item.reportingPeriod}</dd>
            </div>
          )}
          {item.firstSeen && (
            <div>
              <dt>First seen</dt>
              <dd>{formatDate(item.firstSeen)}</dd>
            </div>
          )}
          <div>
            <dt>Affected entity</dt>
            <dd>{item.entity}</dd>
          </div>
        </dl>
        {item.filingChildren && (
          <details className="filing-results">
            <summary>
              Show filing results ({item.filingChildren.length})
            </summary>
            <ul>
              {item.filingChildren.map((row) => (
                <li key={row.label}>
                  <span>{row.label}</span>
                  <strong>{row.value}</strong>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      <button className="source-button" onClick={() => openSource(item.source)}>
        View evidence <ArrowUpRight />
      </button>
    </article>
  );
}

function AssetsDirectory({
  openAsset,
  openSource,
  companyContext,
  assets,
  instruments,
}: {
  openAsset: (id: string) => void;
  openSource: (source: SourceDetail) => void;
  companyContext: string;
  assets: OperatingAssetSummary[];
  instruments: OperatingInstrumentSummary[];
}) {
  const initial =
    typeof window === 'undefined'
      ? new URLSearchParams()
      : new URLSearchParams(window.location.search);
  const [query, setQuery] = useState(initial.get('assetQ') ?? '');
  useEffect(() => {
    const url = new URL(window.location.href);
    if (query) {
      url.searchParams.set('assetQ', query);
    } else {
      url.searchParams.delete('assetQ');
    }
    url.searchParams.delete('assetCompany');
    window.history.replaceState(window.history.state, '', url);
  }, [query]);
  const visible = assets.filter(
    (asset) =>
      `${asset.name} ${asset.company} ${asset.legalFiler} ${asset.cid}`
        .toLowerCase()
        .includes(query.toLowerCase()) &&
      (companyContext === 'All companies' || asset.company === companyContext),
  );
  const active = [query && `Search: ${query}`].filter(Boolean) as string[];
  return (
    <main className="page-shell">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Operating history</p>
          <h1>Operating Assets</h1>
          <p>
            Open an asset to see its key metrics, trends and filing evidence.
          </p>
        </div>
      </div>
      <section className="filter-bar asset-filters single-control">
        <label className="search-field">
          <span>Search</span>
          <div className="input-with-icon">
            <Search />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Company, asset or filing entity"
            />
          </div>
        </label>
      </section>
      <FilterSummary
        items={active}
        clear={() => {
          setQuery('');
        }}
      />
      <div className="results-meta">
        <strong>
          {visible.length} {visible.length === 1 ? 'asset' : 'assets'}
        </strong>
        <span>
          Reviewed directory · filing-entity scope · no parent-company totals
        </span>
      </div>
      <details className="directory-status-guide">
        <summary>What do the data statuses mean?</summary>
        <dl>
          <div>
            <dt>Validated records</dt>
            <dd>
              Some records pass the backend checks. This does not mean every
              metric or period is available.
            </dd>
          </div>
          <div>
            <dt>Quality flags</dt>
            <dd>
              Records carry a unit, scope, source or interpretation
              qualification. A flag is not automatically an open review task.
            </dd>
          </div>
          <div>
            <dt>Need review</dt>
            <dd>
              Records are explicitly queued for human review. They are not
              promoted into key metrics or comparisons.
            </dd>
          </div>
          <div>
            <dt>Identity only / no usable records</dt>
            <dd>
              The asset is listed, but no validated values are available in this
              snapshot. This is not a reported zero.
            </dd>
          </div>
        </dl>
      </details>
      {visible.length ? (
        <div className="directory-list">
          {visible.map((asset) => {
            const latestMetric =
              asset.regime === 'LNG facility' &&
              (asset.scopeRelation === 'shared_filer_entity_context' ||
                ['lng_regas_sendout_capacity', 'lng_storage_capacity'].includes(
                  asset.latestMetric?.metricId || '',
                ))
                ? null
                : asset.latestMetric;
            const qualityFlagCount =
              asset.qualityFlagCount ?? asset.reviewCount;
            const openReviewCount = asset.openReviewCount ?? 0;
            return (
              <Link
                className="asset-row"
                key={asset.id}
                href={routeHref('assets', companyContext, asset.id)}
                prefetch={false}
                onClick={(event) =>
                  followInternalLink(event, () => openAsset(asset.id))
                }
              >
                <div className="asset-monogram">{asset.ticker}</div>
                <div>
                  <div className="row-kicker">
                    {asset.company} · {asset.regime}
                  </div>
                  <h2>{asset.name}</h2>
                  <p>{asset.legalFiler}</p>
                </div>
                <dl>
                  <div>
                    <dt>Latest available period</dt>
                    <dd>{formatDate(asset.latestPeriod ?? undefined)}</dd>
                  </div>
                  <div>
                    <dt>{latestMetric?.label || 'Usable records'}</dt>
                    <dd>
                      {latestMetric
                        ? isMetricPresentationBlocked(
                            latestMetric.metricId,
                            latestMetric.unit,
                            latestMetric.validation,
                          )
                          ? 'Unit conflict — see evidence'
                          : formatBackendValue(
                              latestMetric.value,
                              presentationUnit(
                                latestMetric.metricId,
                                latestMetric.unit,
                                {
                                  configuredDisplayUnit:
                                    latestMetric.configuredDisplayUnit,
                                  displayScale: latestMetric.displayScale,
                                  origin: latestMetric.origin,
                                  validation: latestMetric.validation,
                                },
                              ),
                            )
                        : asset.dataSummary?.usable
                          ? `${asset.dataSummary.usable.toLocaleString()} across ${asset.dataSummary.metrics.toLocaleString()} metrics`
                          : 'None available'}
                      {latestMetric && (
                        <small className="directory-metric-period">
                          {latestMetric.period}
                        </small>
                      )}
                      {asset.scopeRelation ===
                        'shared_filer_entity_context' && (
                        <small className="directory-metric-period">
                          Shared filing-entity figures
                        </small>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Data status</dt>
                    <dd>
                      <Status
                        tone={
                          !asset.dataSummary ||
                          asset.dataSummary.observations === 0 ||
                          asset.dataSummary.usable === 0 ||
                          openReviewCount > 0
                            ? 'warn'
                            : qualityFlagCount > 0
                              ? 'neutral'
                              : 'good'
                        }
                      >
                        {!asset.dataSummary ||
                        asset.dataSummary.observations === 0
                          ? 'Identity only'
                          : openReviewCount > 0
                            ? `${openReviewCount} need review`
                            : qualityFlagCount > 0
                              ? `${qualityFlagCount} quality flags`
                              : asset.dataSummary.usable > 0
                                ? 'Validated records'
                                : 'No usable records'}
                      </Status>
                    </dd>
                  </div>
                </dl>
                <ArrowRight />
              </Link>
            );
          })}
        </div>
      ) : (
        <EmptyState
          filtered
          title={
            companyContext === 'All companies'
              ? undefined
              : `No assets mapped to ${companyContext}`
          }
          description={
            companyContext === 'All companies'
              ? undefined
              : 'Choose another company workspace or All companies. A missing local mapping does not mean a pipeline is inactive.'
          }
        />
      )}
      <RegimeCoverage assets={assets} />
      <ReferenceInstruments instruments={instruments} openSource={openSource} />
    </main>
  );
}

function RegimeCoverage({ assets }: { assets: OperatingAssetSummary[] }) {
  const coverage = [...new Set(assets.map((asset) => asset.regime))]
    .map((regime) => {
      const matching = assets.filter((asset) => asset.regime === regime);
      return {
        regime,
        assets: matching.length,
        mapped: matching.filter(
          (asset) => (asset.dataSummary?.observations ?? 0) > 0,
        ).length,
        observations: matching.reduce(
          (sum, asset) => sum + (asset.dataSummary?.observations || 0),
          0,
        ),
      };
    })
    .sort(
      (left, right) =>
        right.assets - left.assets || left.regime.localeCompare(right.regime),
    );
  return (
    <section className="coverage-panel">
      <div>
        <p className="eyebrow">Coverage boundary</p>
        <h2>Other operating regimes</h2>
        <p>
          The same pinned contract spans multiple filing programs. Coverage
          counts describe this candidate generation, not the universe of FERC
          assets.
        </p>
      </div>
      <div className="coverage-grid">
        {coverage.map((item) => (
          <div key={item.regime}>
            <strong>{item.regime}</strong>
            <span>
              {item.assets} {item.assets === 1 ? 'asset' : 'assets'} ·{' '}
              {item.mapped} with mapped histories
            </span>
            <p>
              {item.observations.toLocaleString()} source-backed observations in
              the pinned generation.
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function ReferenceInstruments({
  instruments,
  openSource,
}: {
  instruments: OperatingInstrumentSummary[];
  openSource: (source: SourceDetail) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [load, setLoad] = useState<{
    key: string;
    detail: OperatingInstrumentDetail | null;
    error: string | null;
  }>({ key: '', detail: null, error: null });
  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    loadFercInstrument(selectedId)
      .then((detail) => {
        if (!cancelled) setLoad({ key: selectedId, detail, error: null });
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          if (reloadForFercSnapshotUpdate(reason)) return;
          setLoad({
            key: selectedId,
            detail: null,
            error:
              reason instanceof Error
                ? reason.message
                : 'The reference instrument could not be opened.',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [attempt, selectedId]);
  if (!instruments.length) return null;
  const selected = instruments.find((item) => item.id === selectedId);
  const detail = load.key === selectedId ? load.detail : null;
  const error = load.key === selectedId ? load.error : null;
  return (
    <section className="reference-instruments">
      <div className="reference-instruments-head">
        <div>
          <p className="eyebrow">Contracted reference data</p>
          <h2>Industry-wide instruments</h2>
          <p>
            These histories are part of the operating-data contract but are not
            carrier assets and are never attributed to a company.
          </p>
        </div>
      </div>
      <div className="reference-instrument-list">
        {instruments.map((instrument) => {
          const qualityFlagCount =
            instrument.qualityFlagCount ?? instrument.reviewCount;
          const openReviewCount = instrument.openReviewCount ?? 0;
          return (
            <article key={instrument.id}>
              <div>
                <span className="status status-neutral">
                  Reference instrument
                </span>
                <h3>{instrument.name}</h3>
                <p>{instrument.jurisdiction || instrument.note}</p>
                <small>
                  {instrument.dataSummary.observations.toLocaleString()}{' '}
                  observations ·{' '}
                  {instrument.dataSummary.usable.toLocaleString()} usable ·{' '}
                  {openReviewCount > 0
                    ? `${openReviewCount.toLocaleString()} need review`
                    : qualityFlagCount > 0
                      ? `${qualityFlagCount.toLocaleString()} quality flags`
                      : 'no open reviews'}{' '}
                  · available through{' '}
                  {formatDate(instrument.latestPeriod ?? undefined)}
                </small>
              </div>
              <button
                className="secondary-button"
                aria-expanded={selectedId === instrument.id}
                onClick={() =>
                  setSelectedId((current) =>
                    current === instrument.id ? null : instrument.id,
                  )
                }
              >
                {selectedId === instrument.id ? 'Hide history' : 'Open history'}
              </button>
            </article>
          );
        })}
      </div>
      {selected && (
        <div className="reference-instrument-detail">
          {error ? (
            <div className="unavailable-inline">
              <CircleAlert />
              <span>{error}</span>
              <button
                className="secondary-button"
                onClick={() => setAttempt((value) => value + 1)}
              >
                Try again
              </button>
            </div>
          ) : !detail ? (
            <div className="lineage-loading">
              Verifying the instrument history…
            </div>
          ) : (
            <>
              <div className="metric-scope-note">
                <Info />
                <p>{detail.instrument.note}</p>
              </div>
              {detail.metrics.map((metric, metricIndex) => (
                <details key={metric.id} open={metricIndex === 0}>
                  <summary>
                    <span>
                      <strong>{metric.label}</strong>
                      <small>
                        {metric.presentCount.toLocaleString()} present ·{' '}
                        {metric.openReviewCount > 0
                          ? `${metric.openReviewCount.toLocaleString()} need review`
                          : metric.reviewCount > 0
                            ? `${metric.reviewCount.toLocaleString()} quality flags`
                            : 'no open reviews'}
                      </small>
                    </span>
                    <ChevronDown />
                  </summary>
                  <p>
                    {metric.description || 'No metric definition supplied.'}
                  </p>
                  <div className="data-table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Period</th>
                          <th>Displayed value</th>
                          <th>Filed value</th>
                          <th>Validation</th>
                          <th>Evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {metric.points.map((point) => (
                          <tr key={point.id}>
                            <td>
                              {point.period.label || point.sortKey || '—'}
                            </td>
                            <td>
                              {formatObservationValue(
                                point,
                                true,
                                metric.id,
                                metric.configuredDisplayUnit,
                              )}
                            </td>
                            <td>{point.value.as_filed || '—'}</td>
                            <td>
                              <Status
                                tone={
                                  point.quality.validation === 'pass' &&
                                  point.quality.version_status !== 'superseded'
                                    ? 'good'
                                    : 'warn'
                                }
                              >
                                {point.quality.validation.replaceAll('_', ' ')}
                              </Status>
                            </td>
                            <td>
                              <button
                                onClick={() =>
                                  openSource(
                                    observationSource(
                                      detail.instrument,
                                      metric,
                                      point,
                                    ),
                                  )
                                }
                              >
                                Details
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              ))}
            </>
          )}
        </div>
      )}
    </section>
  );
}

function ChartPanel({
  title,
  subtitle,
  children,
  table,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
  table: React.ReactNode;
}) {
  return (
    <section className="chart-card">
      <div className="chart-heading">
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
        <Status>Select a mark or use the table</Status>
      </div>
      {children}
      <details className="data-table">
        <summary>View underlying data table</summary>
        <div className="data-table-scroll">{table}</div>
      </details>
    </section>
  );
}

function UnavailableOperatingModules() {
  return (
    <>
      <section className="section-block compact">
        <p className="eyebrow">Shipping rates</p>
        <h2>Published charge detail</h2>
        <div className="module-unavailable">
          <FileText />
          <strong>Rate records not included</strong>
          <p>
            No reviewed route, zone, service, component charge, effective date
            and native-unit records were supplied to this frontend. Revenue per
            unit is not substituted for a shipping quote.
          </p>
        </div>
      </section>
      <section className="section-block compact">
        <p className="eyebrow">Capacity &amp; utilisation</p>
        <h2>Capacity and use evidence</h2>
        <div className="module-unavailable">
          <Database />
          <strong>
            Utilisation not calculated — comparable capacity unavailable
          </strong>
          <p>
            Filed throughput remains available where copied. Contracted
            quantity, certificated capacity and peak-day measures require their
            own denominator, date and scope.
          </p>
        </div>
      </section>
    </>
  );
}

// Retained temporarily as a visual reference for the detached legacy fixture contract.
// oxlint-disable-next-line no-unused-vars
function AssetDetail({
  asset,
  companyContext,
  back,
  openSource,
  openCompare,
  routeWarnings = [],
}: {
  asset: OperatingAssetView;
  companyContext: string;
  back: () => void;
  openSource: (source: SourceDetail) => void;
  openCompare: () => void;
  routeWarnings?: string[];
}) {
  if (!asset.quarters.length) {
    return (
      <main className="page-shell detail-page">
        <Link
          className="back-button"
          href={routeHref('assets', companyContext)}
          prefetch={false}
          onClick={(event) => followInternalLink(event, back)}
        >
          <ArrowLeft /> Back to Assets
        </Link>
        <header className="detail-header">
          <div>
            <div className="detail-badges">
              <Status>{asset.canonicalAssetTypeLabel ?? asset.regime}</Status>
              <Status tone="warn">Metrics not retrieved</Status>
            </div>
            <h1>{asset.name}</h1>
            <p>
              {asset.company} ({asset.ticker}) ·{' '}
              <InfoTerm term="Filing entity">{asset.legalFiler}</InfoTerm> ·{' '}
              <InfoTerm term="CID">{asset.cid}</InfoTerm>
            </p>
          </div>
          <div className="detail-header-actions">
            <button className="compare-button" onClick={openCompare}>
              <GitCompareArrows /> Compare
            </button>
            <div className="refresh-card">
              <span>Identity snapshot</span>
              <strong>{formatDate(asset.snapshotDate)}</strong>
              <small>No operating history copied</small>
            </div>
          </div>
        </header>
        {routeWarnings.length > 0 && (
          <output className="comparison-warning">
            <CircleAlert />
            <div>
              <strong>Comparison could not be restored</strong>
              {routeWarnings.map((warning) => (
                <p key={warning}>{warning}</p>
              ))}
            </div>
          </output>
        )}
        <div className="scope-warning">
          <Info />
          <div>
            <strong>Reporting scope</strong>
            <p>{asset.scope}</p>
          </div>
        </div>
        <section className="section-block identity-only-state">
          <Database />
          <div>
            <p className="eyebrow">Availability</p>
            <h2>Operating metrics not retrieved</h2>
            <p>
              This is a real asset identity from the reviewed universe, not a
              synthetic financial fixture. It can be placed beside compatible
              assets to test missing-data handling; blank cells remain
              unavailable rather than zero.
            </p>
            <button
              className="source-button"
              onClick={() => asset.sources[0] && openSource(asset.sources[0])}
            >
              Inspect mapping source <ArrowUpRight />
            </button>
          </div>
        </section>
        <div className="support-grid">
          <UnavailableOperatingModules />
        </div>
      </main>
    );
  }
  const latest = asset.quarters[asset.quarters.length - 1];
  const prior = asset.quarters.find(
    (point) =>
      point.period ===
      latest.period.replace(
        String(Number(latest.period.slice(0, 4))),
        String(Number(latest.period.slice(0, 4)) - 1),
      ),
  );
  const revenueChange = percentChange(latest.revenue, prior?.revenue ?? null);
  const latestAnnual = asset.annual[asset.annual.length - 1];
  const marginData = asset.quarters.map((point) => ({
    ...point,
    margin: margin(point.income, point.revenue),
  }));
  const exactCurrency = (value: number | null) =>
    value === null ? 'Unavailable' : `$${value.toLocaleString('en-US')}`;
  const exactVolume = (value: number | null) =>
    value === null
      ? 'Unavailable'
      : `${value.toLocaleString('en-US')} FERC Dth`;
  const pointFromEntry = (entry: { payload?: { period?: string } }) =>
    asset.quarters.find((point) => point.period === entry.payload?.period);
  const pointFromPayload = (payload: Record<string, unknown>) =>
    asset.quarters.find((point) => point.period === payload.period);
  const annualPointFromPayload = (payload: Record<string, unknown>) =>
    asset.annual.find((point) => point.year === payload.year);
  const openPoint = (
    point: QuarterPoint | undefined,
    metric: 'Revenue' | 'Operating income' | 'Margin' | 'Throughput',
    comparison?: QuarterPoint,
  ) => {
    if (!point) return;
    const source = sourceForPoint(asset, point);
    if (!source) return;
    if (metric === 'Margin') {
      const value = margin(point.income, point.revenue);
      openSource({
        ...source,
        title: `${asset.name} · Operating margin`,
        value: value === null ? 'Unavailable' : `${value.toFixed(1)}%`,
        valueLabel: 'Displayed derived value',
        unit: '%',
        method: 'Frontend calculation from accepted filed inputs',
        formula:
          'net utility operating income ÷ quarterly operating revenue × 100',
        inputs: [
          {
            label: 'Net utility operating income',
            value: exactCurrency(point.income),
            period: point.period,
            sourceId: point.sourceId,
          },
          {
            label: 'Quarterly operating revenue',
            value: exactCurrency(point.revenue),
            period: point.period,
            sourceId: point.sourceId,
          },
        ],
      });
      return;
    }
    const pointValue =
      metric === 'Revenue'
        ? exactCurrency(point.revenue)
        : metric === 'Operating income'
          ? exactCurrency(point.income)
          : exactVolume(point.throughput);
    const comparisonInputs =
      comparison && metric === 'Revenue'
        ? [
            {
              label: 'Current-quarter revenue',
              value: exactCurrency(point.revenue),
              period: point.period,
              sourceId: point.sourceId,
            },
            {
              label: 'Prior-year same-quarter revenue',
              value: exactCurrency(comparison.revenue),
              period: comparison.period,
              sourceId: comparison.sourceId,
            },
          ]
        : undefined;
    openSource({
      ...source,
      title: `${asset.name} · ${metric}`,
      value: pointValue,
      valueLabel: 'Exact source value',
      unit: metric === 'Throughput' ? 'FERC Dth' : 'USD',
      method: comparisonInputs
        ? 'Filed values; year-on-year comparison formatted in frontend'
        : source.method,
      formula: comparisonInputs
        ? '(current quarter − prior-year same quarter) ÷ prior-year same quarter × 100'
        : source.formula,
      inputs: comparisonInputs,
      description:
        comparisonInputs && revenueChange !== null
          ? `${revenueChange.toFixed(1)}% year-on-year using the two filed inputs below.`
          : source.description,
    });
  };
  const openRow = (point: QuarterPoint) => {
    const source = sourceForPoint(asset, point);
    if (!source) return;
    const marginValue = margin(point.income, point.revenue);
    openSource({
      ...source,
      title: `${asset.name} · ${point.period} filed values`,
      inputs: [
        {
          label: 'Quarterly operating revenue',
          value: exactCurrency(point.revenue),
          period: point.period,
          sourceId: point.sourceId,
        },
        {
          label: 'Net utility operating income',
          value: exactCurrency(point.income),
          period: point.period,
          sourceId: point.sourceId,
        },
        {
          label: 'Total throughput',
          value: exactVolume(point.throughput),
          period: point.period,
          sourceId: point.sourceId,
        },
        {
          label: 'Operating margin (frontend calculation)',
          value:
            marginValue === null ? 'Unavailable' : `${marginValue.toFixed(1)}%`,
          period: point.period,
          sourceId: point.sourceId,
        },
      ],
    });
  };
  const openAnnualPoint = (
    point: AnnualPoint | undefined,
    metric: 'Revenue' | 'Operating income',
  ) => {
    if (!point) return;
    const source = asset.sources.find((item) => item.id.endsWith('annual'));
    if (source)
      openSource({
        ...source,
        title: `${asset.name} · ${point.year} annual ${metric.toLowerCase()}`,
        period: point.year,
        unit: 'USD',
        value: exactCurrency(
          metric === 'Revenue' ? point.revenue : point.income,
        ),
        valueLabel: 'Exact source value',
      });
  };
  const table = (
    <table>
      <thead>
        <tr>
          <th>Period</th>
          <th>Revenue</th>
          <th>Operating income</th>
          <th>Margin</th>
          <th>Throughput</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        {asset.quarters.map((p) => {
          const marginValue = margin(p.income, p.revenue);
          return (
            <tr key={p.period}>
              <td>{p.period}</td>
              <td>
                {p.revenue === null ? '—' : `$${p.revenue.toLocaleString()}`}
              </td>
              <td>
                {p.income === null ? '—' : `$${p.income.toLocaleString()}`}
              </td>
              <td>
                {marginValue === null ? '—' : `${marginValue.toFixed(1)}%`}
              </td>
              <td>{p.throughput?.toLocaleString() ?? '—'}</td>
              <td>
                <button onClick={() => openRow(p)}>Details</button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
  const annualTable = (
    <table>
      <thead>
        <tr>
          <th>Year</th>
          <th>Revenue</th>
          <th>Operating income</th>
          <th>Source detail</th>
        </tr>
      </thead>
      <tbody>
        {asset.annual.map((point) => (
          <tr key={point.year}>
            <td>{point.year}</td>
            <td>{exactCurrency(point.revenue)}</td>
            <td>{exactCurrency(point.income)}</td>
            <td className="table-actions">
              <button
                aria-label={`View ${point.year} revenue source`}
                onClick={() => openAnnualPoint(point, 'Revenue')}
              >
                Revenue
              </button>
              <button
                aria-label={`View ${point.year} operating income source`}
                onClick={() => openAnnualPoint(point, 'Operating income')}
              >
                Income
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <main className="page-shell detail-page">
      <Link
        className="back-button"
        href={routeHref('assets', companyContext)}
        prefetch={false}
        onClick={(event) => followInternalLink(event, back)}
      >
        <ArrowLeft /> Back to Operating Assets
      </Link>
      <header className="detail-header">
        <div>
          <div className="detail-badges">
            <Status>{asset.regime}</Status>
            <Status
              tone={asset.status.validation === 'warning' ? 'warn' : 'good'}
            >
              {asset.status.validation === 'warning'
                ? 'Under review'
                : 'Accepted slice'}
            </Status>
          </div>
          <h1>{asset.name}</h1>
          <p>
            {asset.company} ({asset.ticker}) ·{' '}
            <InfoTerm term="Filing entity">{asset.legalFiler}</InfoTerm> ·{' '}
            <InfoTerm term="CID">{asset.cid}</InfoTerm>
          </p>
        </div>
        <div className="detail-header-actions">
          <button className="compare-button" onClick={openCompare}>
            <GitCompareArrows /> Compare
          </button>
          <div className="refresh-card">
            <span>Operating input snapshot</span>
            <strong>{formatDate(asset.snapshotDate)}</strong>
            <small>Latest copied quarter: {asset.latestCopiedQuarter}</small>
          </div>
        </div>
      </header>
      {routeWarnings.length > 0 && (
        <output className="comparison-warning">
          <CircleAlert />
          <div>
            <strong>Comparison could not be restored</strong>
            {routeWarnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </output>
      )}
      <div className="scope-warning">
        <Info />
        <div>
          <strong>Reporting scope</strong>
          <p>{asset.scope}</p>
        </div>
      </div>
      <section className="what-changed">
        <div>
          <p className="eyebrow">What changed</p>
          <h2>{asset.changes[0].title}</h2>
          <p>{asset.changes[0].detail}</p>
        </div>
        <button
          className="source-button"
          onClick={() => {
            const s = asset.sources.find(
              (source) => source.id === asset.changes[0].sourceId,
            );
            if (s) openSource(s);
          }}
        >
          Inspect note <ArrowUpRight />
        </button>
      </section>
      <section className="headline-grid">
        <button onClick={() => openPoint(latest, 'Revenue', prior)}>
          <span>Quarterly revenue</span>
          <strong>{formatCompact(latest.revenue)}</strong>
          <small>
            {asset.latestCopiedQuarter}
            {revenueChange !== null
              ? ` · ${revenueChange.toFixed(1)}% year-on-year`
              : ''}
          </small>
        </button>
        <button onClick={() => openPoint(latest, 'Operating income')}>
          <span>Net utility operating income</span>
          <strong>{formatCompact(latest.income)}</strong>
          <small>{asset.latestCopiedQuarter} · not EBITDA or cash flow</small>
        </button>
        <button onClick={() => openPoint(latest, 'Margin')}>
          <span>Operating margin</span>
          <strong>{margin(latest.income, latest.revenue)?.toFixed(1)}%</strong>
          <small>Income ÷ revenue · identical filing scope</small>
        </button>
        <button onClick={() => openPoint(latest, 'Throughput')}>
          <span>Total throughput</span>
          <strong>{formatCompact(latest.throughput, 'volume')}</strong>
          <small>FERC Dth · filed system activity</small>
        </button>
      </section>
      <div className="charts-grid">
        <ChartPanel
          title="Quarterly financial performance"
          subtitle="USD millions · quarter-only values; missing values render as gaps"
          table={table}
        >
          <ChartContainer
            config={{
              revenue: { label: 'Revenue', color: '#176b61' },
              income: { label: 'Operating income', color: '#4f70a3' },
            }}
            className="asset-chart"
          >
            <BarChart
              data={asset.quarters.map((p) => ({
                ...p,
                revenue: p.revenue === null ? null : p.revenue / 1e6,
                income: p.income === null ? null : p.income / 1e6,
              }))}
              margin={{ left: 4, right: 12, top: 18, bottom: 4 }}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="period" tickLine={false} />
              <YAxis width={46} tickFormatter={(v) => `$${v}m`} />
              <RechartsTooltip formatter={(v) => `$${Number(v).toFixed(1)}m`} />
              <Legend />
              <Bar
                dataKey="revenue"
                fill="var(--color-revenue)"
                radius={[4, 4, 0, 0]}
                onClick={(entry: { payload?: { period?: string } }) =>
                  openPoint(pointFromEntry(entry), 'Revenue')
                }
              />
              <Bar
                dataKey="income"
                fill="var(--color-income)"
                radius={[4, 4, 0, 0]}
                onClick={(entry: { payload?: { period?: string } }) =>
                  openPoint(pointFromEntry(entry), 'Operating income')
                }
              />
            </BarChart>
          </ChartContainer>
        </ChartPanel>
        <ChartPanel
          title="Operating margin"
          subtitle="Percent · separate scale from currency"
          table={table}
        >
          <ChartContainer
            config={{ margin: { label: 'Operating margin', color: '#176b61' } }}
            className="asset-chart"
          >
            <LineChart
              data={marginData}
              margin={{ left: 4, right: 18, top: 18, bottom: 4 }}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="period" tickLine={false} />
              <YAxis width={42} tickFormatter={(v) => `${v}%`} />
              <RechartsTooltip formatter={(v) => `${Number(v).toFixed(1)}%`} />
              <Line
                dataKey="margin"
                stroke="var(--color-margin)"
                strokeWidth={2.5}
                connectNulls={false}
                dot={(props) => (
                  <SourceDot
                    {...props}
                    color="#176b61"
                    label={(payload) =>
                      `Open ${String(payload.period)} operating margin source`
                    }
                    onSelect={(payload) =>
                      openPoint(pointFromPayload(payload), 'Margin')
                    }
                  />
                )}
              />
            </LineChart>
          </ChartContainer>
        </ChartPanel>
        <ChartPanel
          title="Quarterly total throughput"
          subtitle="Millions of FERC-reported Dth · not physical capacity or utilisation"
          table={table}
        >
          <ChartContainer
            config={{
              throughput: { label: 'Total throughput', color: '#885d83' },
            }}
            className="asset-chart"
          >
            <LineChart
              data={asset.quarters.map((p) => ({
                ...p,
                throughput: p.throughput === null ? null : p.throughput / 1e6,
              }))}
              margin={{ left: 4, right: 18, top: 18, bottom: 4 }}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="period" tickLine={false} />
              <YAxis width={42} tickFormatter={(v) => `${v}m`} />
              <RechartsTooltip
                formatter={(v) => `${Number(v).toFixed(1)}m Dth`}
              />
              <Line
                dataKey="throughput"
                stroke="var(--color-throughput)"
                strokeWidth={2.5}
                connectNulls={false}
                dot={(props) => (
                  <SourceDot
                    {...props}
                    color="#885d83"
                    label={(payload) =>
                      `Open ${String(payload.period)} throughput source`
                    }
                    onSelect={(payload) =>
                      openPoint(pointFromPayload(payload), 'Throughput')
                    }
                  />
                )}
              />
            </LineChart>
          </ChartContainer>
        </ChartPanel>
      </div>
      <section className="section-block">
        <div className="section-title">
          <div>
            <p className="eyebrow">Performance</p>
            <h2>Annual history</h2>
            <p>
              USD billions · annual values remain separate from the copied
              quarterly sample.
            </p>
          </div>
          <Status>Select a mark or use the table</Status>
        </div>
        <ChartContainer
          config={{
            revenue: { label: 'Annual revenue', color: '#176b61' },
            income: { label: 'Annual operating income', color: '#4f70a3' },
          }}
          className="annual-chart"
        >
          <LineChart
            data={asset.annual.map((p) => ({
              ...p,
              revenue: p.revenue / 1e9,
              income: p.income / 1e9,
            }))}
          >
            <CartesianGrid vertical={false} />
            <XAxis dataKey="year" />
            <YAxis width={44} tickFormatter={(v) => `$${v}bn`} />
            <Legend />
            <RechartsTooltip formatter={(v) => `$${Number(v).toFixed(2)}bn`} />
            <Line
              dataKey="revenue"
              stroke="var(--color-revenue)"
              strokeWidth={2.5}
              dot={(props) => (
                <SourceDot
                  {...props}
                  color="#176b61"
                  label={(payload) =>
                    `Open ${String(payload.year)} annual revenue source`
                  }
                  onSelect={(payload) =>
                    openAnnualPoint(annualPointFromPayload(payload), 'Revenue')
                  }
                />
              )}
            />
            <Line
              dataKey="income"
              stroke="var(--color-income)"
              strokeWidth={2.5}
              dot={(props) => (
                <SourceDot
                  {...props}
                  color="#4f70a3"
                  label={(payload) =>
                    `Open ${String(payload.year)} annual operating income source`
                  }
                  onSelect={(payload) =>
                    openAnnualPoint(
                      annualPointFromPayload(payload),
                      'Operating income',
                    )
                  }
                />
              )}
            />
          </LineChart>
        </ChartContainer>
        <div className="annual-latest">
          Latest annual values · 2025{' '}
          <strong>{formatCompact(latestAnnual.revenue)} revenue</strong>
          <strong>{formatCompact(latestAnnual.income)} operating income</strong>
        </div>
        <details className="data-table annual-data-table">
          <summary>View underlying annual data table</summary>
          <div className="data-table-scroll">{annualTable}</div>
        </details>
      </section>
      <div className="support-grid">
        <section className="section-block compact">
          <p className="eyebrow">Contracts</p>
          <h2>Contract position</h2>
          <div className="module-unavailable">
            <Database />
            <strong>Snapshot not retrieved</strong>
            <p>
              <InfoTerm term="MDQ">Firm transportation MDQ</InfoTerm>,{' '}
              <InfoTerm term="Primary-term expiry" /> and{' '}
              <InfoTerm term="Top-five shipper share" /> remain applicable, but
              IOC contract inputs were not copied into this frontend slice.
              Unknown shipper and expiry shares will remain visible when
              integrated.
            </p>
          </div>
        </section>
        <section className="section-block compact">
          <p className="eyebrow">Regulation</p>
          <h2>Rate-case information</h2>
          <div className="module-unavailable">
            <FileText />
            <strong>Source known, detail not retrieved</strong>
            <p>
              No reviewed rate-case/effective-date/refund record was included in
              the selected input. A missing local record does not mean no
              proceeding exists.
            </p>
          </div>
        </section>
        <UnavailableOperatingModules />
      </div>
      <section className="section-block">
        <div className="section-title">
          <div>
            <p className="eyebrow">Sources & details</p>
            <h2>Copied source records</h2>
          </div>
        </div>
        <div className="source-list">
          {asset.sources.slice(-3).map((source) => (
            <button key={source.id} onClick={() => openSource(source)}>
              <FileText />
              <span>
                <strong>{source.title}</strong>
                <small>
                  {source.period} · {source.method}
                </small>
              </span>
              <ArrowRight />
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}

type ComparisonPickerProps = {
  open: boolean;
  anchor: OperatingAssetSummary;
  assets: OperatingAssetSummary[];
  operatingCompanies: string[];
  initialAdditionalIds: string[];
  replacingName?: string;
  requiredAdditionalCount?: number;
  onClose: () => void;
  onConfirm: (ids: string[]) => void;
};

function ComparisonPicker({
  open,
  anchor,
  assets,
  operatingCompanies,
  initialAdditionalIds,
  replacingName,
  requiredAdditionalCount,
  onClose,
  onConfirm,
}: ComparisonPickerProps) {
  const [query, setQuery] = useState('');
  const [company, setCompany] = useState('All companies');
  const [selected, setSelected] = useState<string[]>(initialAdditionalIds);

  const requiredType =
    anchor.canonicalAssetTypeLabel ?? 'reviewed canonical type unavailable';
  const replacing = Boolean(replacingName);
  const lockedIds = replacing ? initialAdditionalIds : [];
  const maximumAdditional = replacing
    ? (requiredAdditionalCount ?? initialAdditionalIds.length + 1)
    : MAX_COMPARISON_ASSETS - 1;
  const selectedAssets = selected
    .map((id) => assets.find((asset) => asset.id === id))
    .filter(Boolean) as OperatingAssetSummary[];
  const eligible = assets
    .filter((asset) => isComparisonEligible(anchor, asset))
    .filter((asset) => company === 'All companies' || asset.company === company)
    .filter((asset) =>
      `${asset.name} ${asset.company} ${asset.legalFiler} ${asset.cid}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    )
    .sort((left, right) => {
      const leftPeer = left.company === anchor.company ? 0 : 1;
      const rightPeer = right.company === anchor.company ? 0 : 1;
      return leftPeer - rightPeer || left.name.localeCompare(right.name);
    });
  const selectedCount = selected.length + 1;
  const canConfirm =
    selectedCount >= 2 && (!replacing || selected.length === maximumAdditional);
  const toggle = (assetId: string) => {
    if (assetId === anchor.id || lockedIds.includes(assetId)) return;
    setSelected((current) => {
      if (current.includes(assetId)) {
        return current.filter((id) => id !== assetId);
      }
      if (current.length >= maximumAdditional) return current;
      return [...current, assetId];
    });
  };

  return (
    <Sheet open={open} onOpenChange={(next) => !next && onClose()}>
      <SheetContent className="comparison-picker sm:max-w-[580px]">
        <SheetHeader className="comparison-picker-head">
          <p className="eyebrow">Optional comparison</p>
          <SheetTitle>
            {replacingName ? `Replace ${replacingName}` : 'Choose assets'}
          </SheetTitle>
          <SheetDescription>
            {replacing
              ? `Choose one new ${requiredType.toLowerCase()} asset. The open asset and other comparison assets stay in place.`
              : `The open asset stays first. Select up to three additional ${requiredType.toLowerCase()} assets.`}
          </SheetDescription>
        </SheetHeader>
        <div className="picker-controls">
          <label className="search-field">
            <span>Search all available companies</span>
            <div className="input-with-icon">
              <Search />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Asset, company, filer or CID"
              />
            </div>
          </label>
          <label>
            <span>Company</span>
            <select
              value={company}
              onChange={(event) => setCompany(event.target.value)}
            >
              <option>All companies</option>
              {operatingCompanies.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>
        <div className="picker-type-note">
          <Info />
          <span>
            Required type: <strong>{requiredType}</strong>. Assets must also
            share an exact metric, period basis, base unit and scope contract.
          </span>
        </div>
        {selectedAssets.length > 0 && (
          <div
            className="picker-selected"
            aria-label="Selected additional assets"
          >
            <span>Selected</span>
            <div>
              {selectedAssets.map((asset) => {
                const locked = lockedIds.includes(asset.id);
                return (
                  <button
                    type="button"
                    key={asset.id}
                    disabled={locked}
                    aria-label={
                      locked
                        ? `${asset.name} remains selected`
                        : `Remove ${asset.name} from selection`
                    }
                    onClick={() => toggle(asset.id)}
                  >
                    {asset.name}
                    {locked ? <small>Kept</small> : <X />}
                  </button>
                );
              })}
            </div>
          </div>
        )}
        <fieldset className="picker-results" aria-label="Eligible assets">
          {eligible.length ? (
            eligible.map((asset) => {
              const isAnchor = asset.id === anchor.id;
              const checked = isAnchor || selected.includes(asset.id);
              const locked = lockedIds.includes(asset.id);
              const atLimit = !checked && selected.length >= maximumAdditional;
              return (
                <button
                  type="button"
                  key={asset.id}
                  className={
                    checked ? 'picker-result selected' : 'picker-result'
                  }
                  aria-pressed={checked}
                  disabled={isAnchor || locked || atLimit}
                  onClick={() => toggle(asset.id)}
                >
                  <span className="picker-check">
                    {checked ? <Check /> : null}
                  </span>
                  <span>
                    <strong>{asset.name}</strong>
                    <small>
                      {asset.company} · {asset.canonicalAssetTypeLabel}
                    </small>
                    <em>
                      {asset.legalFiler} · {asset.cid}
                    </em>
                  </span>
                  {isAnchor ? (
                    <b>Open asset</b>
                  ) : locked ? (
                    <b>Already selected</b>
                  ) : null}
                </button>
              );
            })
          ) : (
            <EmptyState
              filtered={Boolean(query || company !== 'All companies')}
              title={query ? 'No eligible matches' : 'No eligible peers'}
              description={`No ${requiredType.toLowerCase()} asset matches this picker search. Try All companies or clear the search.`}
            />
          )}
        </fieldset>
        <div className="picker-footer">
          <span>
            <strong>
              {selectedCount} of {MAX_COMPARISON_ASSETS}
            </strong>{' '}
            assets selected
            {!replacing && selectedCount === MAX_COMPARISON_ASSETS
              ? ' · maximum reached'
              : ''}
          </span>
          <div>
            <button className="secondary-button" onClick={onClose}>
              Cancel
            </button>
            <button
              className="primary-button"
              disabled={!canConfirm}
              onClick={() => onConfirm(selected)}
            >
              {replacing ? 'Apply replacement' : 'Compare selected'}
            </button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

const comparisonColors = ['#176b61', '#4f70a3', '#885d83', '#73815b'];
const comparisonDashes = [undefined, '8 4', '3 3', '10 3 2 3'];

// Retained temporarily as a visual reference while legacy fixture URLs migrate.
// oxlint-disable-next-line no-unused-vars
function AssetComparison({
  assets,
  issues,
  back,
  add,
  replace,
  remove,
  exit,
  openSource,
}: {
  assets: OperatingAssetView[];
  issues: string[];
  back: () => void;
  add: () => void;
  replace: (id: string) => void;
  remove: (id: string) => void;
  exit: () => void;
  openSource: (source: SourceDetail) => void;
}) {
  const anchor = assets[0];
  const periods = assets.reduce<string[]>((common, asset, index) => {
    const own = asset.quarters.map((point) => point.period);
    return index === 0 ? own : common.filter((period) => own.includes(period));
  }, []);
  const commonPeriod = periods.at(-1);
  const crossCompany = new Set(assets.map((asset) => asset.company)).size > 1;
  const mappingWarnings = assets
    .map((asset) => asset.canonicalAssetTypeWarning)
    .filter(Boolean) as string[];
  const chartPeriods = [
    ...new Set(
      assets.flatMap((asset) => asset.quarters.map((point) => point.period)),
    ),
  ].sort();
  const chartData = chartPeriods.map((period) => {
    const row: Record<string, string | number | null> = { period };
    assets.forEach((asset, index) => {
      const point = asset.quarters.find((item) => item.period === period);
      row[`revenue${index}`] = point?.revenue ?? null;
      row[`margin${index}`] = point
        ? margin(point.income, point.revenue)
        : null;
      row[`throughput${index}`] = point?.throughput ?? null;
    });
    return row;
  });
  const pointFor = (asset: OperatingAssetView) =>
    commonPeriod
      ? asset.quarters.find((point) => point.period === commonPeriod)
      : asset.quarters.at(-1);
  const openComparisonSource = (
    asset: OperatingAssetView,
    point: QuarterPoint,
    metric: 'Revenue' | 'Operating income' | 'Margin' | 'Throughput',
  ) => {
    const value =
      metric === 'Revenue'
        ? point.revenue
        : metric === 'Operating income'
          ? point.income
          : metric === 'Throughput'
            ? point.throughput
            : margin(point.income, point.revenue);
    const matchedSource = sourceForPoint(asset, point);
    const source: SourceDetail = matchedSource ?? {
      id: `unresolved-${asset.id}-${point.period}`,
      title: `${asset.name} · source reference unavailable`,
      sourceSystem: 'Detached frontend snapshot',
      nativeIdentity: point.sourceId,
      period: point.period,
      scope: `${asset.legalFiler}; ${asset.scope}`,
      method: 'No matching source detail supplied',
      availability: 'Source reference unresolved',
      origin: 'Copied observation',
      validation: 'Not assessed',
      description:
        'The observation is retained, but its referenced source detail is not present in this detached frontend snapshot. No substitute source has been attached.',
      warnings: [
        `Referenced source ID ${point.sourceId} could not be resolved in the copied source list.`,
      ],
    };
    const summaryValue = commonPeriod === point.period;
    openSource({
      ...source,
      title: `${asset.name} · ${metric}`,
      period: point.period,
      scope: `${asset.legalFiler}; ${asset.scope}`,
      unit:
        metric === 'Margin'
          ? '%'
          : metric === 'Throughput'
            ? 'FERC Dth'
            : 'USD',
      value:
        value === null
          ? 'Unavailable'
          : metric === 'Margin'
            ? `${value.toFixed(1)}%`
            : metric === 'Throughput'
              ? `${value.toLocaleString('en-US')} FERC Dth`
              : `$${value.toLocaleString('en-US')}`,
      valueLabel: summaryValue
        ? 'Value at latest common comparable quarter'
        : commonPeriod
          ? 'Historical observation outside the comparison summary basis'
          : 'Latest separately labelled available value',
      formula:
        metric === 'Margin'
          ? 'net utility operating income ÷ quarterly operating revenue × 100'
          : source.formula,
      inputs:
        metric === 'Margin'
          ? [
              {
                label: 'Net utility operating income',
                value:
                  point.income === null
                    ? 'Unavailable'
                    : `$${point.income.toLocaleString('en-US')}`,
                period: point.period,
                sourceId: point.sourceId,
              },
              {
                label: 'Quarterly operating revenue',
                value:
                  point.revenue === null
                    ? 'Unavailable'
                    : `$${point.revenue.toLocaleString('en-US')}`,
                period: point.period,
                sourceId: point.sourceId,
              },
            ]
          : source.inputs,
      warnings: [
        ...(source.warnings ?? []),
        ...(point.warning ? [point.warning] : []),
      ],
    });
  };
  const valueSource = (
    asset: OperatingAssetView,
    metric: 'Revenue' | 'Operating income' | 'Margin' | 'Throughput',
  ) => {
    const point = pointFor(asset);
    if (point) openComparisonSource(asset, point, metric);
  };
  const metricRows = [
    {
      label: 'Quarterly revenue',
      metric: 'Revenue' as const,
      format: (point?: QuarterPoint) => formatCompact(point?.revenue ?? null),
      note: 'USD · filing entity',
    },
    {
      label: 'Net utility operating income',
      metric: 'Operating income' as const,
      format: (point?: QuarterPoint) => formatCompact(point?.income ?? null),
      note: 'USD · not EBITDA or cash flow',
    },
    {
      label: 'Operating margin',
      metric: 'Margin' as const,
      format: (point?: QuarterPoint) => {
        const value = point ? margin(point.income, point.revenue) : null;
        return value === null ? 'Unavailable' : `${value.toFixed(1)}%`;
      },
      note: 'Income ÷ revenue · same source scope',
    },
    {
      label: 'Total throughput',
      metric: 'Throughput' as const,
      format: (point?: QuarterPoint) =>
        formatCompact(point?.throughput ?? null, 'volume'),
      note: 'FERC Dth · not capacity or utilisation',
    },
  ];
  const comparisonTable = (metric: 'revenue' | 'margin' | 'throughput') => (
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
        {chartPeriods.map((period) => (
          <tr key={period}>
            <td>{period}</td>
            {assets.map((asset) => {
              const point = asset.quarters.find(
                (item) => item.period === period,
              );
              const sourceMetric =
                metric === 'revenue'
                  ? ('Revenue' as const)
                  : metric === 'throughput'
                    ? ('Throughput' as const)
                    : ('Margin' as const);
              const value =
                metric === 'revenue'
                  ? point?.revenue
                  : metric === 'throughput'
                    ? point?.throughput
                    : point
                      ? margin(point.income, point.revenue)
                      : null;
              const displayValue =
                value === null || value === undefined
                  ? 'Unavailable'
                  : metric === 'margin'
                    ? `${value.toFixed(1)}%`
                    : metric === 'throughput'
                      ? `${value.toLocaleString('en-US')} FERC Dth`
                      : `$${value.toLocaleString('en-US')}`;
              return (
                <td key={asset.id}>
                  {point ? (
                    <button
                      className="comparison-table-source"
                      aria-label={`Open ${asset.name} ${period} ${sourceMetric.toLowerCase()} source details`}
                      onClick={() =>
                        openComparisonSource(asset, point, sourceMetric)
                      }
                    >
                      <span>{displayValue}</span>
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
  );

  return (
    <main className="page-shell detail-page comparison-page">
      <button className="back-button" onClick={back}>
        <ArrowLeft /> Back to asset
      </button>
      <header className="comparison-titlebar">
        <div>
          <div className="detail-badges">
            <Status>{anchor.canonicalAssetTypeLabel}</Status>
            {crossCompany && (
              <Status tone="project">Cross-company comparison</Status>
            )}
          </div>
          <h1 id="asset-comparison-title">Asset comparison</h1>
          <p>
            {assets.length} of {MAX_COMPARISON_ASSETS} assets · original asset
            stays first · values retain each filing entity’s scope
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
            <strong>Some comparison selections could not be restored</strong>
            {issues.map((issue) => (
              <p key={issue}>{issue}</p>
            ))}
          </div>
        </output>
      )}
      {mappingWarnings.length > 0 && (
        <output className="comparison-warning">
          <CircleAlert />
          <div>
            <strong>Identity mapping note</strong>
            {mappingWarnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </output>
      )}
      <section className="comparison-basis">
        <div>
          <span>Summary basis</span>
          <strong>
            {commonPeriod
              ? `Latest common comparable quarter · ${commonPeriod}`
              : 'No common comparable reporting period'}
          </strong>
        </div>
        <p>
          {commonPeriod
            ? 'Newer observations outside the shared quarter remain in source histories and are not used in the aligned rows below.'
            : 'Latest available values remain visible with their own periods; no difference, ranking or substituted period is calculated.'}
        </p>
      </section>
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
          <div className="comparison-corner">Performance</div>
          {assets.map((asset, index) => (
            <div className="comparison-asset-head" key={asset.id}>
              <i style={{ background: comparisonColors[index] }} />
              <small>{asset.company}</small>
              <strong>{asset.name}</strong>
              <span>{asset.legalFiler}</span>
              <em>{asset.scope}</em>
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
          {metricRows.flatMap((row) => [
            <div className="comparison-row-label" key={`${row.metric}-label`}>
              <strong>{row.label}</strong>
              <span>{row.note}</span>
            </div>,
            ...assets.map((asset) => {
              const point = pointFor(asset);
              return (
                <button
                  key={`${row.metric}-${asset.id}`}
                  className="comparison-value"
                  disabled={!point}
                  aria-label={
                    point
                      ? `${row.label} for ${asset.name}: ${row.format(point)}, ${point.period}. Open source details.`
                      : `${row.label} for ${asset.name}: not retrieved.`
                  }
                  onClick={() => valueSource(asset, row.metric)}
                >
                  <strong>{row.format(point)}</strong>
                  <span>{point?.period ?? 'Not retrieved'}</span>
                  {point?.warning && <em>Review note in source</em>}
                </button>
              );
            }),
          ])}
          <div className="comparison-row-label section-label">
            <strong>Shipping rates</strong>
            <span>Route, service, date and native units required</span>
          </div>
          <div className="comparison-span-row">
            Not included in the supplied frontend contract. Historical revenue
            per unit is not presented as a published transportation charge.
          </div>
          <div className="comparison-row-label section-label">
            <strong>Capacity &amp; utilisation</strong>
            <span>Denominator, date and scope must match</span>
          </div>
          <div className="comparison-span-row">
            Utilisation not calculated — comparable capacity unavailable.
            Available filed throughput remains visible above.
          </div>
          <div className="comparison-row-label section-label">
            <strong>Contracts &amp; regulation</strong>
            <span>Snapshots and order dates remain separate</span>
          </div>
          <div className="comparison-span-row">
            Contract MDQ, expiry, concentration and rate-case records were not
            copied for these assets. Their absence is not treated as zero or no
            proceeding.
          </div>
        </div>
      </section>
      <div className="comparison-charts">
        {(
          [
            [
              'revenue',
              'Quarterly operating revenue',
              'USD millions · quarter-only values',
              (v: number) => v / 1e6,
              (v: number) => `$${v}m`,
            ],
            [
              'margin',
              'Operating margin',
              'Percent · income divided by revenue on identical asset scope',
              (v: number) => v,
              (v: number) => `${v}%`,
            ],
            [
              'throughput',
              'Quarterly total throughput',
              'Millions of FERC-reported Dth · not capacity or utilisation',
              (v: number) => v / 1e6,
              (v: number) => `${v}m`,
            ],
          ] as const
        ).map(([metric, title, subtitle, scale, tick]) => (
          <ChartPanel
            key={metric}
            title={title}
            subtitle={subtitle}
            table={comparisonTable(metric)}
          >
            {chartPeriods.length ? (
              <ChartContainer config={{}} className="comparison-chart">
                <LineChart
                  data={chartData.map((row) => {
                    const scaled = { ...row };
                    assets.forEach((_, index) => {
                      const key = `${metric}${index}`;
                      const value = scaled[key];
                      scaled[key] =
                        typeof value === 'number' ? scale(value) : null;
                    });
                    return scaled;
                  })}
                  margin={{ left: 8, right: 20, top: 20, bottom: 4 }}
                >
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="period" />
                  <YAxis width={52} tickFormatter={tick} />
                  <RechartsTooltip formatter={(value) => tick(Number(value))} />
                  <Legend />
                  {assets.map((asset, index) => (
                    <Line
                      key={asset.id}
                      type="linear"
                      dataKey={`${metric}${index}`}
                      name={asset.name}
                      stroke={comparisonColors[index]}
                      strokeDasharray={comparisonDashes[index]}
                      strokeWidth={2.5}
                      connectNulls={false}
                      dot={(props) => (
                        <SourceDot
                          {...props}
                          color={comparisonColors[index]}
                          label={(payload) =>
                            `Open ${asset.name} ${String(payload.period)} ${title.toLowerCase()} source`
                          }
                          onSelect={(payload) => {
                            const point = asset.quarters.find(
                              (item) => item.period === payload.period,
                            );
                            if (point) {
                              openComparisonSource(
                                asset,
                                point,
                                metric === 'revenue'
                                  ? 'Revenue'
                                  : metric === 'throughput'
                                    ? 'Throughput'
                                    : 'Margin',
                              );
                            }
                          }}
                        />
                      )}
                    />
                  ))}
                </LineChart>
              </ChartContainer>
            ) : (
              <div className="chart-empty">
                No compatible history was copied for the selected assets.
              </div>
            )}
          </ChartPanel>
        ))}
      </div>
    </main>
  );
}

function ProjectsDirectory({
  openProject,
  companyContext,
}: {
  openProject: (docket: string) => void;
  companyContext: string;
}) {
  const initial =
    typeof window === 'undefined'
      ? new URLSearchParams()
      : new URLSearchParams(window.location.search);
  const initialStage = initial.get('projectStage');
  const [query, setQuery] = useState(initial.get('projectQ') ?? '');
  const [stage, setStage] = useState(
    initialStage && projectStages.includes(initialStage)
      ? initialStage
      : 'All stages',
  );
  useEffect(() => {
    const url = new URL(window.location.href);
    if (query) {
      url.searchParams.set('projectQ', query);
    } else {
      url.searchParams.delete('projectQ');
    }
    if (stage !== 'All stages') {
      url.searchParams.set('projectStage', stage);
    } else {
      url.searchParams.delete('projectStage');
    }
    url.searchParams.delete('projectCompany');
    window.history.replaceState(window.history.state, '', url);
  }, [query, stage]);
  const visible = projects.filter(
    (project) =>
      `${project.project} ${project.docket}`
        .toLowerCase()
        .includes(query.toLowerCase()) &&
      (stage === 'All stages' || project.current_stage === stage) &&
      (companyContext === 'All companies' ||
        project.company === companyContext),
  );
  return (
    <main className="page-shell">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Regulatory development</p>
          <h1>Projects</h1>
          <p>
            Filing-based project stages, milestone timelines and supporting
            accessions.
          </p>
        </div>
      </div>
      <section className="filter-bar project-filters">
        <label className="search-field">
          <span>Search</span>
          <div className="input-with-icon">
            <Search />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Project name or docket"
            />
          </div>
        </label>
        <label>
          <span>Stage assessment</span>
          <select value={stage} onChange={(e) => setStage(e.target.value)}>
            <option>All stages</option>
            {projectStages.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
      </section>
      <FilterSummary
        items={
          [query && `Search: ${query}`, stage !== 'All stages' && stage].filter(
            Boolean,
          ) as string[]
        }
        clear={() => {
          setQuery('');
          setStage('All stages');
        }}
      />
      <div className="results-meta">
        <strong>{visible.length} projects</strong>
        <span>
          Backend snapshots generated 9 Sep 2026 · not independently verified
        </span>
      </div>
      {visible.length ? (
        <div className="project-grid">
          {visible.map((project) => {
            const assessed =
              project.regulatory_outlook?.latest_material_milestone;
            const latest =
              assessed ??
              [...project.milestones].sort((left, right) =>
                right.date.localeCompare(left.date),
              )[0];
            return (
              <Link
                className="project-card"
                key={project.docket}
                href={routeHref('projects', companyContext, project.docket)}
                prefetch={false}
                onClick={(event) =>
                  followInternalLink(event, () => openProject(project.docket))
                }
              >
                <div className="project-card-top">
                  <Status tone="project">{project.docket}</Status>
                  <Status>{project.current_stage}</Status>
                </div>
                <h2>{project.project}</h2>
                <p className="company-unmapped">
                  Company not mapped in supplied project export
                </p>
                <div className="milestone-preview">
                  <span>
                    {assessed
                      ? 'Latest material milestone'
                      : 'Latest supplied milestone'}
                  </span>
                  <strong>{latest?.title ?? 'Unavailable'}</strong>
                  <small>{formatDate(latest?.date)}</small>
                </div>
                <div className="request-row">
                  {project.regulatory_outlook ? (
                    <>
                      <span>
                        {project.regulatory_outlook.responded_ferc_requests}{' '}
                        responses matched
                      </span>
                      <span>
                        {project.regulatory_outlook.tracked_ferc_requests}{' '}
                        tracked requests
                      </span>
                    </>
                  ) : (
                    <span className="warning-text">
                      Regulatory outlook unavailable
                    </span>
                  )}
                  <ArrowRight />
                </div>
              </Link>
            );
          })}
        </div>
      ) : (
        <EmptyState
          filtered
          title={
            companyContext === 'All companies'
              ? undefined
              : `No projects mapped to ${companyContext}`
          }
          description={
            companyContext === 'All companies'
              ? undefined
              : 'The supplied Tommy project snapshots do not include reviewed parent-company mappings. Choose Company not mapped or All companies to see them.'
          }
        />
      )}
    </main>
  );
}

function ProjectDetail({
  project,
  companyContext,
  back,
  openSource,
}: {
  project: ProjectView;
  companyContext: string;
  back: () => void;
  openSource: (source: SourceDetail) => void;
}) {
  return (
    <main className="page-shell detail-page">
      <Link
        className="back-button"
        href={routeHref('projects', companyContext)}
        prefetch={false}
        onClick={(event) => followInternalLink(event, back)}
      >
        <ArrowLeft /> Back to Projects
      </Link>
      <header className="detail-header project-detail-header">
        <div>
          <div className="detail-badges">
            <Status tone="project">{project.docket}</Status>
            <Status>{project.current_stage}</Status>
          </div>
          <h1>{project.project}</h1>
          <p>
            Company not mapped ·{' '}
            <InfoTerm term="Docket">{project.docket}</InfoTerm>
          </p>
        </div>
        <div className="refresh-card">
          <span>Snapshot generated</span>
          <strong>{formatDate(project.generated_at)}</strong>
          <small>Generation time is not proof of source freshness</small>
        </div>
      </header>
      <div className="scope-warning">
        <Info />
        <div>
          <strong>Filing-based assessment</strong>
          <p>
            The supplied current stage is derived from classified filings. It is
            not automatically a formal FERC-issued status, construction
            progress, or percent complete.
          </p>
        </div>
      </div>
      <div className="project-layout">
        <div className="project-main">
          <section className="section-block">
            <div className="section-title">
              <div>
                <p className="eyebrow">Milestones</p>
                <h2>Regulatory timeline</h2>
              </div>
            </div>
            <ol className="timeline">
              {project.milestones.map((item, index) => (
                <li key={item.accession}>
                  <div className="timeline-rail">
                    <i />
                    <span>{index < project.milestones.length - 1 && ''}</span>
                  </div>
                  <div>
                    <time>{formatDate(item.date)}</time>
                    <h3>{item.title}</h3>
                    <p>{item.description}</p>
                    <button
                      className="source-button"
                      onClick={() =>
                        openSource(
                          projectSource(
                            project,
                            item.accession,
                            item.title,
                            item.date,
                            item.description,
                          ),
                        )
                      }
                    >
                      Accession {item.accession} <ArrowUpRight />
                    </button>
                  </div>
                </li>
              ))}
            </ol>
          </section>
          <section className="section-block">
            <div className="section-title">
              <div>
                <p className="eyebrow">Grouped activity</p>
                <h2>Supporting filing activity</h2>
                <p>
                  Showing {project.activity.length} copied representative groups
                  of {project.counts.activity_groups}; accessions are
                  selections, not necessarily the full docket archive.
                </p>
              </div>
            </div>
            <div className="activity-list">
              {project.activity.map((activity, index) => (
                <details
                  key={`${activity.start_date}-${index}`}
                  open={index === 0}
                >
                  <summary>
                    <span>
                      <Status>
                        {activity.filing_count} filing
                        {activity.filing_count === 1 ? '' : 's'}
                      </Status>
                      <strong>{activity.label}</strong>
                      <small>
                        {formatDate(activity.start_date)}
                        {activity.end_date !== activity.start_date
                          ? ` – ${formatDate(activity.end_date)}`
                          : ''}
                      </small>
                    </span>
                    <ChevronDown />
                  </summary>
                  <div>
                    <p>{activity.summary}</p>
                    <dl>
                      <div>
                        <dt>Grouping relationship</dt>
                        <dd>{activity.relationship.replaceAll('_', ' ')}</dd>
                      </div>
                      <div>
                        <dt>Classifier categories</dt>
                        <dd>
                          {Object.entries(activity.event_types)
                            .map(
                              ([key, value]) =>
                                `${key.replaceAll('_', ' ')} (${value})`,
                            )
                            .join(', ')}
                        </dd>
                      </div>
                    </dl>
                    <div className="accession-row">
                      <span>
                        Representative{' '}
                        <InfoTerm term="Accession">accessions</InfoTerm>
                      </span>
                      {activity.representative_accessions.map((accession) => (
                        <button
                          key={accession}
                          onClick={() =>
                            openSource(
                              projectSource(
                                project,
                                accession,
                                activity.label,
                                undefined,
                                activity.summary,
                              ),
                            )
                          }
                        >
                          {accession}
                        </button>
                      ))}
                    </div>
                  </div>
                </details>
              ))}
            </div>
          </section>
        </div>
        <aside className="project-aside">
          <section
            className={`outlook-card ${project.regulatory_outlook ? '' : 'unavailable'}`}
          >
            <p className="eyebrow">Regulatory outlook</p>
            {project.regulatory_outlook ? (
              <>
                <div className="outlook-status">
                  <span>Backend assessment</span>
                  <strong>
                    {project.regulatory_outlook.regulatory_status}
                  </strong>
                </div>
                <p>{project.regulatory_outlook.investor_summary}</p>
                <div className="request-stats">
                  <div>
                    <strong>
                      {project.regulatory_outlook.tracked_ferc_requests}
                    </strong>
                    <span>Tracked requests</span>
                  </div>
                  <div>
                    <strong>
                      {project.regulatory_outlook.responded_ferc_requests}
                    </strong>
                    <span>Responses matched</span>
                  </div>
                  <div>
                    <strong>
                      {project.regulatory_outlook.open_ferc_requests}
                    </strong>
                    <span>Unmatched in export</span>
                  </div>
                </div>
                <p className="fine-print">
                  Matched responses do not prove FERC accepted or closed the
                  requests.
                </p>
                <div className="next-activity">
                  <Clock3 />
                  <div>
                    <span>Next expected activity</span>
                    <strong>
                      {project.regulatory_outlook.next_expected_activity}
                    </strong>
                  </div>
                </div>
              </>
            ) : (
              <>
                <CircleAlert />
                <h2>Assessment unavailable</h2>
                <p>
                  The supplied <code>regulatory_outlook</code> is null. This
                  does not mean zero open requests or “all clear.”
                </p>
              </>
            )}
          </section>
          {project.regulatory_outlook?.schedule_watch.length ? (
            <section className="watch-card">
              <div className="watch-heading">
                <CalendarDays />
                <div>
                  <span>Schedule watch</span>
                  <strong>Timing uncertain</strong>
                </div>
              </div>
              {project.regulatory_outlook.schedule_watch.map((watch) => (
                <div className="watch-item" key={watch.request_accession}>
                  <p>{watch.detail}</p>
                  <dl>
                    <div>
                      <dt>Request</dt>
                      <dd>{formatDate(watch.request_date)}</dd>
                    </div>
                    <div>
                      <dt>Estimated due</dt>
                      <dd>{formatDate(watch.estimated_due_date)}</dd>
                    </div>
                    <div>
                      <dt>Response</dt>
                      <dd>{formatDate(watch.response_date)}</dd>
                    </div>
                  </dl>
                  <small>
                    Estimated date · not a confirmed legal deadline or delay
                  </small>
                </div>
              ))}
            </section>
          ) : null}
          <details className="diagnostics">
            <summary>
              <Database /> Snapshot diagnostics <ChevronDown />
            </summary>
            <dl>
              <div>
                <dt>Raw records</dt>
                <dd>{project.counts.raw}</dd>
              </div>
              <div>
                <dt>Review</dt>
                <dd>{project.counts.review}</dd>
              </div>
              <div>
                <dt>Suppressed</dt>
                <dd>{project.counts.suppressed}</dd>
              </div>
              <div>
                <dt>Activity groups</dt>
                <dd>{project.counts.activity_groups}</dd>
              </div>
            </dl>
            <p>Classifier counts are diagnostics, not project-health scores.</p>
          </details>
        </aside>
      </div>
    </main>
  );
}

function RouteNotFound({
  kind,
  identifier,
  companyContext,
  go,
}: {
  kind: 'asset' | 'project';
  identifier: string;
  companyContext: string;
  go: () => void;
}) {
  const label = kind === 'asset' ? 'Operating Assets' : 'Projects';
  return (
    <main className="page-shell">
      <div className="empty-state route-not-found">
        <CircleAlert />
        <h1>{kind === 'asset' ? 'Asset' : 'Project'} not found</h1>
        <p>
          No copied development record matches <code>{identifier}</code>. This
          does not imply that the underlying asset, project, or FERC record does
          not exist.
        </p>
        <Link
          className="primary-link"
          href={routeHref(
            kind === 'asset' ? 'assets' : 'projects',
            companyContext,
          )}
          prefetch={false}
          onClick={(event) => followInternalLink(event, go)}
        >
          Return to {label}
        </Link>
      </div>
    </main>
  );
}

function DataLoadState({
  title,
  description,
  error = false,
  retry,
}: {
  title: string;
  description: string;
  error?: boolean;
  retry?: () => void;
}) {
  return (
    <main className="page-shell">
      <section className={`data-load-state${error ? ' data-load-error' : ''}`}>
        {error ? <CircleAlert /> : <Database />}
        <div>
          <p className="eyebrow">
            {error ? 'Data unavailable' : 'Verified snapshot'}
          </p>
          <h1>{title}</h1>
          <p>{description}</p>
          {retry && (
            <button className="secondary-button" onClick={retry}>
              <RefreshCw /> Try again
            </button>
          )}
        </div>
      </section>
    </main>
  );
}

function Footer({ catalog }: { catalog: FercCatalog | null }) {
  return (
    <footer>
      <div>
        <span>Operating inputs</span>
        <strong>
          {catalog
            ? `Receipt-pinned candidate · ${formatDate(catalog.directory.asOf)}`
            : 'Operating snapshot unavailable'}
        </strong>
        <small>
          {catalog
            ? `Generation ${catalog.manifest.generationId.slice(0, 12)} · contract ${catalog.manifest.source.contractVersion}`
            : 'No fallback operating values are shown'}
        </small>
      </div>
      <div>
        <span>Project inputs</span>
        <strong>
          {sourceProvenance.projects.label} · {sourceProvenance.projects.date}
        </strong>
        <small>{sourceProvenance.projects.identity}</small>
      </div>
    </footer>
  );
}

export default function Home() {
  const [historyRevision, setHistoryRevision] = useState(0);
  const [route, setRoute] = useState<RouteState>({
    view: 'changes',
    company: 'All companies',
    compareIds: [],
  });
  const [source, setSource] = useState<SourceDetail | null>(null);
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  const [catalog, setCatalog] = useState<FercCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [operatingChanges, setOperatingChanges] = useState<ChangeItem[] | null>(
    null,
  );
  const [changeLoadError, setChangeLoadError] = useState<string | null>(null);
  const [changeLoadAttempt, setChangeLoadAttempt] = useState(0);
  const [assetLoad, setAssetLoad] = useState<{
    key: string;
    details: OperatingAssetDetail[] | null;
    error: string | null;
  }>({ key: '', details: null, error: null });
  const [assetDetailAttempt, setAssetDetailAttempt] = useState(0);
  const viewScroll = useRef<Record<ViewName, number>>({
    assets: 0,
    projects: 0,
    changes: 0,
  });
  const [picker, setPicker] = useState<{
    initialIds: string[];
    replacingName?: string;
    requiredAdditionalCount?: number;
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    loadFercCatalog()
      .then((value) => {
        if (!cancelled) setCatalog(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          if (reloadForFercSnapshotUpdate(error)) return;
          setCatalogError(
            error instanceof Error
              ? error.message
              : 'The FERC snapshot could not be opened.',
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [catalogAttempt]);
  useEffect(() => {
    const apply = () => setRoute(readRoute());
    const timer = window.setTimeout(apply, 0);
    const handler = (event: PopStateEvent) => {
      setSource(null);
      setGlossaryOpen(false);
      setPicker(null);
      // Remount URL-backed filters only on history navigation, never typing.
      setHistoryRevision((revision) => revision + 1);
      apply();
      setTimeout(() => window.scrollTo(0, event.state?.scrollY ?? 0), 0);
    };
    window.addEventListener('popstate', handler);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('popstate', handler);
    };
  }, []);
  const operatingAssets = useMemo(() => catalog?.assets ?? [], [catalog]);
  const operatingCompanies = useMemo(
    () => [...new Set(operatingAssets.map((asset) => asset.company))].sort(),
    [operatingAssets],
  );
  const changes = useMemo(
    () => [
      ...fixtureChanges.filter((item) => item.domain === 'Projects'),
      ...(operatingChanges ?? []),
    ],
    [operatingChanges],
  );
  const changeRegimes = useMemo(
    () => [...new Set(changes.map((item) => item.regime))].sort(),
    [changes],
  );
  const workspaceCompanies = useMemo(
    () =>
      [...new Set([...operatingCompanies, ...projectCompanies])].sort(
        (left, right) => {
          if (left === 'Company not mapped') return 1;
          if (right === 'Company not mapped') return -1;
          return left.localeCompare(right);
        },
      ),
    [operatingCompanies],
  );
  useEffect(() => {
    if (!catalog) return;
    const canonicalAsset = route.asset
      ? resolveAssetId(route.asset)
      : undefined;
    const canonicalCompare = route.compareIds.map(resolveAssetId);
    const validCompany =
      route.company === 'All companies' ||
      workspaceCompanies.includes(route.company)
        ? route.company
        : 'All companies';
    const changed =
      canonicalAsset !== route.asset ||
      canonicalCompare.some((id, index) => id !== route.compareIds[index]) ||
      validCompany !== route.company;
    if (!changed) return;
    const next = new URL(window.location.href);
    if (canonicalAsset) next.searchParams.set('asset', canonicalAsset);
    if (canonicalCompare.length)
      next.searchParams.set('compare', canonicalCompare.join(','));
    else next.searchParams.delete('compare');
    if (validCompany === 'All companies') next.searchParams.delete('company');
    else next.searchParams.set('company', validCompany);
    window.history.replaceState(window.history.state, '', next);
    const timer = window.setTimeout(() => {
      setRoute((current) => ({
        ...current,
        asset: canonicalAsset,
        compareIds: canonicalCompare,
        company: validCompany,
      }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    catalog,
    route.asset,
    route.compareIds,
    route.company,
    workspaceCompanies,
  ]);
  useEffect(() => {
    if (!catalog || route.view !== 'changes') return;
    let cancelled = false;
    loadFercChanges()
      .then((value) => {
        if (!cancelled) setOperatingChanges(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          if (reloadForFercSnapshotUpdate(error)) return;
          setChangeLoadError(
            error instanceof Error
              ? error.message
              : 'The change archive could not be opened.',
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [catalog, changeLoadAttempt, route.view]);
  const go = (view: ViewName, id?: string) => {
    setSource(null);
    setGlossaryOpen(false);
    const current = new URL(window.location.href);
    const leavingDirectory =
      route.view === 'changes' ||
      (route.view === 'assets' && !route.asset) ||
      (route.view === 'projects' && !route.project);
    if (leavingDirectory) viewScroll.current[route.view] = window.scrollY;
    window.history.replaceState(
      { ...window.history.state, scrollY: window.scrollY, fercApp: true },
      '',
      current,
    );
    current.searchParams.set('view', view);
    current.searchParams.delete('asset');
    current.searchParams.delete('project');
    current.searchParams.delete('compare');
    current.searchParams.delete('assetCompany');
    current.searchParams.delete('projectCompany');
    current.searchParams.delete('changeCompany');
    if (route.company === 'All companies') {
      current.searchParams.delete('company');
    } else {
      current.searchParams.set('company', route.company);
    }
    if (view === 'assets' && id) current.searchParams.set('asset', id);
    if (view === 'projects' && id) current.searchParams.set('project', id);
    window.history.pushState(
      { scrollY: 0, fercApp: true, fercReturnView: id ? view : undefined },
      '',
      current,
    );
    setRoute({
      view,
      asset: view === 'assets' ? id : undefined,
      project: view === 'projects' ? id : undefined,
      company: route.company,
      compareIds: [],
    });
    window.setTimeout(
      () => window.scrollTo(0, id ? 0 : viewScroll.current[view]),
      0,
    );
  };
  const setCompany = (company: string) => {
    setSource(null);
    setGlossaryOpen(false);
    setPicker(null);
    const next = new URL(window.location.href);
    window.history.replaceState(
      { ...window.history.state, scrollY: window.scrollY, fercApp: true },
      '',
      next,
    );
    if (company === 'All companies') next.searchParams.delete('company');
    else next.searchParams.set('company', company);
    next.searchParams.delete('assetCompany');
    next.searchParams.delete('projectCompany');
    next.searchParams.delete('changeCompany');
    next.searchParams.delete('asset');
    next.searchParams.delete('project');
    next.searchParams.delete('compare');
    window.history.pushState({ scrollY: 0, fercApp: true }, '', next);
    setRoute({ view: route.view, company, compareIds: [] });
    window.scrollTo(0, 0);
  };
  const clearComparison = () => {
    setSource(null);
    setPicker(null);
    if (window.history.state?.fercComparisonFromAsset) {
      window.history.back();
      return;
    }
    const next = new URL(window.location.href);
    next.searchParams.delete('compare');
    window.history.replaceState({ scrollY: 0, fercApp: true }, '', next);
    setRoute({ ...route, compareIds: [] });
    window.scrollTo(0, 0);
  };
  const setComparison = (additionalIds: string[]) => {
    if (!route.asset) return;
    if (!additionalIds.length) {
      clearComparison();
      return;
    }
    setSource(null);
    const next = new URL(window.location.href);
    next.searchParams.set('compare', additionalIds.join(','));
    const state = {
      scrollY: 0,
      fercApp: true,
      fercReturnView: 'assets',
      fercComparisonFromAsset:
        route.compareIds.length === 0 ||
        Boolean(window.history.state?.fercComparisonFromAsset),
    };
    if (route.compareIds.length > 0) {
      window.history.replaceState(state, '', next);
    } else {
      window.history.pushState(state, '', next);
    }
    setRoute({ ...route, compareIds: additionalIds });
    setPicker(null);
    window.scrollTo(0, 0);
  };
  const backTo = (view: 'assets' | 'projects') => {
    setSource(null);
    setGlossaryOpen(false);
    if (window.history.state?.fercReturnView === view) {
      window.history.back();
    } else {
      go(view);
    }
  };
  const requestedAssetId = route.asset
    ? resolveAssetId(route.asset)
    : undefined;
  const asset = requestedAssetId
    ? operatingAssets.find((item) => item.id === requestedAssetId)
    : undefined;
  const project = route.project
    ? projects.find((item) => item.docket === route.project)
    : undefined;
  const comparison = asset
    ? validateComparisonSelection(asset, route.compareIds, operatingAssets)
    : undefined;
  const comparisonWarnings =
    route.compareIds.length > 0
      ? (comparison?.issues.map((issue) => issue.message) ?? [])
      : [];
  const additionalAssets = comparison?.assets.slice(1) ?? [];
  const detailIds = comparison?.assets.map((item) => item.id) ?? [];
  const detailKey = detailIds.join('|');
  const assetRequestKey = `${detailKey}@${assetDetailAttempt}`;
  useEffect(() => {
    if (!catalog || !asset) return;
    let cancelled = false;
    Promise.all(
      detailKey
        .split('|')
        .filter(Boolean)
        .map((id) => loadFercAsset(id)),
    )
      .then((details) => {
        if (!cancelled) {
          setAssetLoad({ key: assetRequestKey, details, error: null });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          if (reloadForFercSnapshotUpdate(error)) return;
          setAssetLoad({
            key: assetRequestKey,
            details: null,
            error:
              error instanceof Error
                ? error.message
                : 'The asset payload could not be opened.',
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [asset, assetRequestKey, catalog, detailKey]);
  const assetDetails =
    assetLoad.key === assetRequestKey ? assetLoad.details : null;
  const assetDetailError =
    assetLoad.key === assetRequestKey ? assetLoad.error : null;
  return (
    <div className="app-frame">
      <AppHeader
        route={route}
        go={go}
        setCompany={setCompany}
        openGlossary={() => setGlossaryOpen(true)}
        workspaceCompanies={workspaceCompanies}
        catalog={catalog}
      />
      {route.view === 'changes' &&
        (catalogError ? (
          <DataLoadState
            error
            title="Operating snapshot unavailable"
            description={`${catalogError} Project fixtures are not substituted into the combined feed.`}
            retry={() => {
              setCatalogError(null);
              setCatalogAttempt((value) => value + 1);
            }}
          />
        ) : changeLoadError ? (
          <DataLoadState
            error
            title="FERC change archive unavailable"
            description={`${changeLoadError} Project fixtures are not substituted into the combined feed.`}
            retry={() => {
              setChangeLoadError(null);
              setChangeLoadAttempt((value) => value + 1);
            }}
          />
        ) : catalog && operatingChanges ? (
          <ChangesView
            key={historyRevision}
            openSource={setSource}
            companyContext={route.company}
            changes={changes}
            changeRegimes={changeRegimes}
          />
        ) : catalog ? (
          <DataLoadState
            title="Loading the FERC change archive"
            description="Verifying and opening the historical archive separately from the asset directory."
          />
        ) : (
          <DataLoadState
            title="Verifying the FERC snapshot"
            description="Checking the pinned manifest and catalog integrity before showing any operating records."
          />
        ))}{' '}
      {route.view === 'assets' &&
        (catalogError ? (
          <DataLoadState
            error
            title="Operating assets unavailable"
            description={catalogError}
            retry={() => {
              setCatalogError(null);
              setCatalogAttempt((value) => value + 1);
            }}
          />
        ) : !catalog ? (
          <DataLoadState
            title="Verifying the asset directory"
            description="Checking the receipt-pinned catalog before showing asset identities or metrics."
          />
        ) : route.asset ? (
          asset ? (
            assetDetailError ? (
              <DataLoadState
                error
                title="Asset history unavailable"
                description={`${assetDetailError} No static fallback values were substituted.`}
                retry={() => setAssetDetailAttempt((value) => value + 1)}
              />
            ) : !assetDetails ? (
              <DataLoadState
                title={
                  comparison && comparison.assets.length > 1
                    ? 'Loading comparable histories'
                    : 'Loading source-backed history'
                }
                description="Verifying the selected asset payload against the pinned manifest."
              />
            ) : comparison && comparison.assets.length > 1 ? (
              <BackendAssetComparison
                details={assetDetails}
                issues={comparisonWarnings}
                back={clearComparison}
                add={() =>
                  setPicker({
                    initialIds: additionalAssets.map((item) => item.id),
                  })
                }
                replace={(id) => {
                  const replaced = additionalAssets.find(
                    (item) => item.id === id,
                  );
                  setPicker({
                    initialIds: additionalAssets
                      .filter((item) => item.id !== id)
                      .map((item) => item.id),
                    replacingName: replaced?.name,
                    requiredAdditionalCount: additionalAssets.length,
                  });
                }}
                remove={(id) =>
                  setComparison(
                    additionalAssets
                      .filter((item) => item.id !== id)
                      .map((item) => item.id),
                  )
                }
                exit={clearComparison}
                openSource={setSource}
              />
            ) : (
              <BackendAssetDetail
                key={assetDetails[0].asset.id}
                detail={assetDetails[0]}
                companyContext={route.company}
                back={() => backTo('assets')}
                openSource={setSource}
                openCompare={() => setPicker({ initialIds: [] })}
                routeWarnings={comparisonWarnings}
              />
            )
          ) : (
            <RouteNotFound
              kind="asset"
              identifier={route.asset}
              companyContext={route.company}
              go={() => go('assets')}
            />
          )
        ) : (
          <AssetsDirectory
            key={historyRevision}
            openAsset={(id) => go('assets', id)}
            openSource={setSource}
            companyContext={route.company}
            assets={operatingAssets}
            instruments={catalog.directory.instruments}
          />
        ))}{' '}
      {route.view === 'projects' &&
        (route.project ? (
          project ? (
            <ProjectDetail
              project={project}
              companyContext={route.company}
              back={() => backTo('projects')}
              openSource={setSource}
            />
          ) : (
            <RouteNotFound
              kind="project"
              identifier={route.project}
              companyContext={route.company}
              go={() => go('projects')}
            />
          )
        ) : (
          <ProjectsDirectory
            key={historyRevision}
            openProject={(id) => go('projects', id)}
            companyContext={route.company}
          />
        ))}
      <Footer catalog={catalog} />
      <SourceDrawer source={source} onClose={() => setSource(null)} />
      <GlossaryDrawer
        open={glossaryOpen}
        onClose={() => setGlossaryOpen(false)}
      />
      {asset && picker && (
        <ComparisonPicker
          open
          anchor={asset}
          assets={operatingAssets}
          operatingCompanies={operatingCompanies}
          initialAdditionalIds={picker?.initialIds ?? []}
          replacingName={picker?.replacingName}
          requiredAdditionalCount={picker?.requiredAdditionalCount}
          onClose={() => setPicker(null)}
          onConfirm={setComparison}
        />
      )}
    </div>
  );
}
