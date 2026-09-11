-- ============================================================================
-- FERC all-regime operating-asset STAGING schema.
--
-- One shared store for every source regime (gas/liquids XBRL, IOC, capacity
-- documents, Form 549D bulk tables, eLibrary documents). Adapters never define
-- their own tables: they emit rows against this contract, and the single
-- integrating writer commits them.
--
-- Four rules the shape of this schema exists to enforce:
--
--   1. EXACT SOURCE OCCURRENCE. A fact is identified by
--      (source_system, filing_id, source_fact_id). Content identifiers repeat
--      across byte-identical resubmissions, so content hash alone is not an
--      identity. Accession is eLibrary's key and is NULL for other systems.
--
--   2. PERIOD GRAIN IS PART OF IDENTITY. An observation is keyed by entity,
--      metric, regime, period_basis, the exact interval/instant, scope and
--      unit. A year-to-date fact can therefore never silently satisfy a
--      requested quarter -- the defect the Form 2-A audit found.
--
--   3. FIVE INDEPENDENT STATUS DIMENSIONS. availability / origin / method /
--      version_status / validation are separate columns. A value can be
--      migrated + derived + revised + valid-with-a-unit-warning at once.
--
--   4. BLANK IS NOT ZERO, AND ABSENT IS NOT UNAVAILABLE. A source blank, a nil
--      fact, a not-required schedule, a retrieval failure and an unimplemented
--      adapter are five different availability states and never collapse.
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- run state

CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  mode              TEXT NOT NULL,            -- discover | backfill | refresh | validate | export
  scope_json        TEXT NOT NULL,            -- the filters this run was invoked with
  registry_version  TEXT NOT NULL,
  code_version      TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'running',   -- running | complete | failed | interrupted
  note              TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
  run_id     TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  ts         TEXT NOT NULL,
  level      TEXT NOT NULL,                   -- info | warn | error
  adapter    TEXT,
  entity_cid TEXT,
  message    TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

-- Per-(adapter, entity, period) resume state. A context limit or a crash must
-- not lose completed work or restart a successful backfill.
CREATE TABLE IF NOT EXISTS checkpoints (
  adapter        TEXT NOT NULL,
  entity_cid     TEXT NOT NULL,
  scope_key      TEXT NOT NULL,               -- e.g. "Form 2:2025:Q4" or "ioc:2026-07-01"
  state          TEXT NOT NULL,               -- pending | in_progress | done | failed | skipped
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  last_run_id    TEXT,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (adapter, entity_cid, scope_key)
);

-- Append-only per-run evidence. `checkpoints` is the mutable resume pointer;
-- these rows are the durable chronology and must never be replaced by a later
-- IOC-only or narrow-window run.
CREATE TABLE IF NOT EXISTS run_unit_status (
  run_id         TEXT NOT NULL,
  adapter        TEXT NOT NULL,
  entity_cid     TEXT NOT NULL,
  scope_key      TEXT NOT NULL,
  seq            INTEGER NOT NULL,
  state          TEXT NOT NULL,
  detail         TEXT,
  recorded_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, adapter, entity_cid, scope_key, seq)
);

CREATE TABLE IF NOT EXISTS run_input_inventory (
  run_id          TEXT NOT NULL,
  adapter         TEXT NOT NULL,
  entity_cid      TEXT NOT NULL,
  input_order     INTEGER NOT NULL,
  source_system   TEXT,
  filing_id       TEXT,
  accession_number TEXT,
  content_hash    TEXT,
  submitted_on    TEXT,
  form            TEXT,
  reporting_year  INTEGER,
  reporting_period TEXT,
  snapshot_date   TEXT,
  version_status  TEXT,
  is_canonical    INTEGER,
  observed_at     TEXT NOT NULL,
  PRIMARY KEY (run_id, adapter, entity_cid, input_order)
);

-- Written in the SAME transaction as an entity/adapter/window publication.
-- On startup this distinguishes "process died after COMMIT but before status"
-- from "process died before/during COMMIT" without guessing from row values.
CREATE TABLE IF NOT EXISTS unit_commits (
  run_id          TEXT NOT NULL,
  adapter         TEXT NOT NULL,
  entity_cid      TEXT NOT NULL,
  scope_key       TEXT NOT NULL,
  identity_json   TEXT NOT NULL,
  input_digest    TEXT NOT NULL,
  observation_count INTEGER NOT NULL,
  edge_count      INTEGER NOT NULL,
  document_fact_count INTEGER NOT NULL,
  committed_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, adapter, entity_cid, scope_key)
);

CREATE INDEX IF NOT EXISTS ix_run_unit_status_current
  ON run_unit_status(run_id, state, adapter, entity_cid);
CREATE INDEX IF NOT EXISTS ix_run_inputs_filing
  ON run_input_inventory(source_system, filing_id, run_id);

-- One row is inserted only after a complete file generation has been staged
-- and verified. The external receipt carries this id and the per-file hashes,
-- tying consumer exports to the same run/code/input boundary as the database.
CREATE TABLE IF NOT EXISTS publication_generations (
  generation_id    TEXT PRIMARY KEY,
  run_id           TEXT,
  code_snapshot    TEXT NOT NULL,
  input_snapshot   TEXT NOT NULL,
  database_identity TEXT NOT NULL,
  manifest_json    TEXT NOT NULL,
  status           TEXT NOT NULL,
  published_at     TEXT NOT NULL
);

-- ---------------------------------------------------------------- identity

CREATE TABLE IF NOT EXISTS entities (
  entity_key     TEXT PRIMARY KEY,            -- FERC CID, or a reviewed local key
  cid            TEXT,                        -- NULL when FERC publishes no CID
  local_key      TEXT,                        -- reviewed local identity, else NULL
  legal_name     TEXT NOT NULL,
  parent         TEXT,
  ticker         TEXT,
  jurisdiction   TEXT,
  note           TEXT
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id         TEXT PRIMARY KEY,
  ticker           TEXT,
  display_name     TEXT NOT NULL,
  template         TEXT NOT NULL,             -- interstate_gas | gas_storage | liquids |
                                              -- intrastate_549d | lng | OUT_OF_TEMPLATE
  authority        TEXT,
  cod_group        TEXT,
  group_key        TEXT,
  status           TEXT,
  note             TEXT
);

-- Many-to-many and effective-dated: one filing entity can serve several assets
-- and one asset can involve several entities. mapping_scope records what the
-- entity's filed figures actually cover, so a filing-entity total is never
-- silently duplicated across physical assets or rolled into a parent.
CREATE TABLE IF NOT EXISTS asset_entity_map (
  asset_id       TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  entity_key     TEXT NOT NULL REFERENCES entities(entity_key) ON DELETE CASCADE,
  mapping_scope  TEXT NOT NULL,               -- whole_entity | facility_subset | shared_facility
  effective_from TEXT,
  effective_to   TEXT,
  note           TEXT,
  PRIMARY KEY (asset_id, entity_key)
);

-- Ownership is a LABEL, never a multiplier. Fayetteville Express is 50% JV and
-- still reports 100% entity/system data: we show the label, not halved volumes.
CREATE TABLE IF NOT EXISTS ownership (
  entity_key          TEXT NOT NULL REFERENCES entities(entity_key) ON DELETE CASCADE,
  parent              TEXT NOT NULL,
  ticker              TEXT,
  pct                 REAL,
  basis               TEXT NOT NULL,          -- direct | jv | effective | via-holdco | undisclosed
  qualifier           TEXT,
  effective_from      TEXT,
  effective_to        TEXT,
  PRIMARY KEY (entity_key, parent)
);

CREATE TABLE IF NOT EXISTS dockets (
  docket    TEXT PRIMARY KEY,
  prefix    TEXT,
  note      TEXT
);

CREATE TABLE IF NOT EXISTS asset_dockets (
  asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  docket   TEXT NOT NULL,
  role     TEXT NOT NULL,
  evidence_ref TEXT,                         -- versioned routing/source evidence
  PRIMARY KEY (asset_id, docket)
);

-- ---------------------------------------------------------------- sources

-- A filing occurrence. Identical resubmissions each get their own row: history
-- is retained, and only an actual content change is an economic change.
CREATE TABLE IF NOT EXISTS filings (
  source_system     TEXT NOT NULL,            -- eCollection_XBRL | eLibrary | DataFERC | ...
  filing_id         TEXT NOT NULL,            -- the source's own native record ID
  entity_key        TEXT NOT NULL,
  form              TEXT NOT NULL,            -- Form 2 | Form 2A | Form 3Q Gas | Form 6 | ...
  accession_number  TEXT,                     -- eLibrary only; NULL elsewhere
  reporting_year    INTEGER,
  reporting_period  TEXT,                     -- Q1..Q4 | annual | snapshot date
  period_start      TEXT,
  period_end        TEXT,
  -- FERC distinguishes these; we keep every one it supplies, separately.
  filed_date        TEXT,
  posted_date       TEXT,
  issued_date       TEXT,
  effective_date    TEXT,
  submitted_on      TEXT,
  snapshot_date     TEXT,                     -- IOC header snapshot; NOT the report date
  acceptance_status TEXT,
  taxonomy_version  TEXT,
  schema_ref        TEXT,
  content_hash      TEXT,                     -- sha256 of the retrieved bytes
  is_canonical      INTEGER NOT NULL DEFAULT 0,
  canonical_reason  TEXT,
  version_status    TEXT,                     -- original | identical_resubmission | revised |
                                              -- superseded | unresolved
  supersedes_filing_id TEXT,
  data_origin       TEXT,                     -- native_xbrl | ferc_migrated | structured_bulk | document
  retrieved_at      TEXT,
  first_seen_at     TEXT,
  source_url        TEXT,
  PRIMARY KEY (source_system, filing_id)
);

CREATE INDEX IF NOT EXISTS ix_filings_entity ON filings(entity_key, form, reporting_year);
CREATE INDEX IF NOT EXISTS ix_filings_accession ON filings(accession_number);

-- A filing occurrence can name or govern more than one legal entity.  The
-- compatibility anchor in filings.entity_key is deterministic but deliberately
-- non-exclusive; this junction is the semantic association.  In particular,
-- joint LNG operating reports and Commission orders must not change ownership
-- merely because entity replay order changes.
CREATE TABLE IF NOT EXISTS filing_entities (
  source_system    TEXT NOT NULL,
  filing_id        TEXT NOT NULL,
  entity_key       TEXT NOT NULL,
  association_role TEXT NOT NULL,            -- source_entity | named_filer |
                                             -- commission_docket_subject |
                                             -- facility_subject | compatibility_anchor
  facility_key     TEXT,
  evidence_ref     TEXT NOT NULL,
  PRIMARY KEY (source_system, filing_id, entity_key),
  FOREIGN KEY (source_system, filing_id)
    REFERENCES filings(source_system, filing_id) ON DELETE CASCADE,
  FOREIGN KEY (entity_key) REFERENCES entities(entity_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_filing_entities_entity
  ON filing_entities(entity_key, source_system, filing_id);

CREATE TABLE IF NOT EXISTS filing_dockets (
  source_system TEXT NOT NULL,
  filing_id     TEXT NOT NULL,
  docket        TEXT NOT NULL,
  PRIMARY KEY (source_system, filing_id, docket)
);

-- One filing may carry several attachments. Attachment ID and content hash are
-- deliberately separate columns: the same bytes can appear under two IDs.
CREATE TABLE IF NOT EXISTS documents (
  document_id     TEXT PRIMARY KEY,           -- source_system|filing_id|attachment_id
  source_system   TEXT NOT NULL,
  filing_id       TEXT NOT NULL,
  accession_number TEXT,
  attachment_id   TEXT,
  title           TEXT,
  class_type      TEXT,
  media_type      TEXT,
  byte_size       INTEGER,
  content_hash    TEXT,                       -- sha256 of the attachment bytes
  cache_path      TEXT,                       -- content-addressed cache location
  text_layer      TEXT,                       -- yes | no | partial | unknown
  availability    TEXT,                       -- retrieved | nonpublic | not_retrieved | failed
  retrieved_at    TEXT,
  source_url      TEXT
);

CREATE INDEX IF NOT EXISTS ix_documents_filing ON documents(source_system, filing_id);

-- Raw XBRL / structured facts, exactly as filed. Values are never repaired.
CREATE TABLE IF NOT EXISTS source_facts (
  source_system    TEXT NOT NULL,
  filing_id        TEXT NOT NULL,
  source_fact_id   TEXT NOT NULL,
  document_order   INTEGER,
  concept_qname    TEXT,
  concept_local    TEXT,
  context_id       TEXT,
  unit_id          TEXT,
  unit_text        TEXT,
  decimals         TEXT,
  precision        TEXT,
  value_as_filed   TEXT,                      -- exact bytes; "" for self-closing
  is_nil           INTEGER NOT NULL DEFAULT 0,
  period_class     TEXT,                      -- instant|monthly|quarter|ytd|annual|other_*
  instant          TEXT,
  period_start     TEXT,
  period_end       TEXT,
  duration_days    TEXT,
  current_or_prior TEXT,
  explicit_dims_json TEXT,
  typed_dims_json    TEXT,
  taxonomy_version TEXT,
  PRIMARY KEY (source_system, filing_id, source_fact_id)
);

CREATE INDEX IF NOT EXISTS ix_facts_concept ON source_facts(concept_local, filing_id);

CREATE TABLE IF NOT EXISTS source_contexts (
  source_system TEXT NOT NULL,
  filing_id     TEXT NOT NULL,
  context_id    TEXT NOT NULL,
  entity_identifier TEXT,
  scheme        TEXT,
  instant       TEXT,
  period_start  TEXT,
  period_end    TEXT,
  period_class  TEXT,
  duration_days TEXT,
  containers    TEXT,
  n_explicit    INTEGER,
  n_typed       INTEGER,
  PRIMARY KEY (source_system, filing_id, context_id)
);

CREATE TABLE IF NOT EXISTS source_dimensions (
  source_system TEXT NOT NULL,
  filing_id     TEXT NOT NULL,
  context_id    TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  dim_kind      TEXT NOT NULL,                -- explicit | typed
  container     TEXT,
  axis_qname    TEXT,
  axis_local    TEXT,
  member_qname  TEXT,
  member_local  TEXT,
  typed_value   TEXT,
  PRIMARY KEY (source_system, filing_id, context_id, seq)
);

CREATE TABLE IF NOT EXISTS source_units (
  source_system TEXT NOT NULL,
  filing_id     TEXT NOT NULL,
  unit_id       TEXT NOT NULL,
  numerators    TEXT,
  denominators  TEXT,
  normalized    TEXT,
  PRIMARY KEY (source_system, filing_id, unit_id)
);

-- Every retrieved byte-stream with its URL and checksum.
CREATE TABLE IF NOT EXISTS source_manifest (
  cache_key     TEXT PRIMARY KEY,             -- sha256 of redacted retrieval URL/cache key
  content_hash  TEXT NOT NULL,                -- sha256 of the immutable response bytes
  source_system TEXT NOT NULL,
  source_url    TEXT NOT NULL,
  media_type    TEXT,
  byte_size     INTEGER,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  fetch_count   INTEGER NOT NULL DEFAULT 1,
  note          TEXT
);

-- ---------------------------------------------------------------- canonical

-- The canonical observation. See rules 2 and 3 in the header.
CREATE TABLE IF NOT EXISTS observations (
  observation_id   TEXT PRIMARY KEY,
  run_id           TEXT,
  entity_key       TEXT NOT NULL,
  metric_id        TEXT NOT NULL,
  source_regime    TEXT NOT NULL,             -- the form/dataset that supplied it
  -- period grain: basis + exact interval/instant. Part of identity.
  period_basis     TEXT NOT NULL,             -- instant | quarter | ytd | annual | monthly |
                                              -- snapshot | as_of | annual_observation
  period_start     TEXT,
  period_end       TEXT,
  instant_date     TEXT,
  reporting_year   INTEGER,
  reporting_period TEXT,
  period_label     TEXT,
  scope            TEXT NOT NULL,             -- regulatory scope of the figure
  scope_rule       TEXT,                      -- registry instruction; never used as actual scope
  unit             TEXT,
  value_text       TEXT,                      -- exactly as filed
  value_num        REAL,                      -- parsed; NULL when not numeric or blank
  normalized_iso   TEXT,                      -- for date-valued metrics
  -- five independent status dimensions (rule 3)
  availability     TEXT NOT NULL,             -- see ferclib.status.Availability
  origin           TEXT NOT NULL,
  method           TEXT NOT NULL,
  version_status   TEXT NOT NULL,
  validation       TEXT NOT NULL,
  -- provenance
  source_system    TEXT,
  filing_id        TEXT,
  source_fact_id   TEXT,
  source_context_id TEXT,
  document_id      TEXT,
  accession_number TEXT,
  candidate_count  INTEGER,
  selector         TEXT,
  derivation       TEXT,
  concept_local    TEXT,
  concept_qname    TEXT,
  taxonomy_version TEXT,
  registry_version TEXT,
  applicability_version TEXT,
  schedule_page    TEXT,
  taxonomy_label   TEXT,
  -- QA, carried through to every export (Form 2-A audit finding B)
  qa_flags         TEXT,                      -- ';'-joined; must survive into exports
  review_status    TEXT,                      -- '' | open | resolved
  missing_reason   TEXT,
  applicability_evidence TEXT,
  notes            TEXT,
  first_seen_at    TEXT,
  updated_at       TEXT
);

-- Rule 2 in one constraint: a YTD row and a quarter row are different
-- observations even for the same metric and quarter label. Re-running the same
-- input therefore updates in place rather than duplicating.
CREATE UNIQUE INDEX IF NOT EXISTS ux_observation_grain ON observations(
  entity_key, metric_id, source_regime, period_basis,
  IFNULL(period_start,''), IFNULL(period_end,''), IFNULL(instant_date,''),
  scope, IFNULL(unit,''), method
);

CREATE INDEX IF NOT EXISTS ix_obs_entity_metric ON observations(entity_key, metric_id);
CREATE INDEX IF NOT EXISTS ix_obs_filing ON observations(source_system, filing_id);

-- Derived values carry an edge to every input, including denominator and
-- group-membership evidence for ratios and shares.
CREATE TABLE IF NOT EXISTS lineage_edges (
  observation_id     TEXT NOT NULL REFERENCES observations(observation_id) ON DELETE CASCADE,
  input_order        INTEGER NOT NULL,
  input_role         TEXT NOT NULL,           -- minuend | subtrahend | addend | numerator |
                                              -- denominator | group_member | basis
  operator_sign      TEXT,
  coefficient        REAL,
  input_source_system TEXT,
  input_filing_id    TEXT,
  input_source_fact_id TEXT,
  input_observation_id TEXT,
  input_context_id   TEXT,
  input_concept      TEXT,
  input_period       TEXT,
  input_value        TEXT,
  input_unit         TEXT,
  input_version_status TEXT,
  -- Set-based lineage. Some aggregates have thousands of contributing source
  -- rows (an IOC contract book, a 549D billing population), where one edge per
  -- row is neither storable nor readable. Such an aggregate carries ONE edge
  -- with input_role='population' pointing at a lineage_populations row that
  -- defines the set exactly and reproducibly. This is the ONLY permitted
  -- alternative to per-row edges -- see A08.
  input_population_id TEXT REFERENCES lineage_populations(population_id),
  PRIMARY KEY (observation_id, input_order)
);

-- A prior version of a canonical observation, kept because the observation id
-- is the GRAIN: a revision overwrites the current row, and the value the filer
-- previously reported must still be recoverable. Written only when the
-- replacement actually differs, so an unchanged refresh archives nothing.
--
-- Added for A01 clause 4. The audit's negative control "canonical revision
-- history preserves previous observation value" found 1 row where 2 were
-- expected: with the id equal to the grain there was simply nowhere for the
-- superseded value to go. A deterministic current view and preserved prior
-- provenance are not in tension -- they just need two tables.
CREATE TABLE IF NOT EXISTS observation_versions (
  observation_id   TEXT NOT NULL,
  version_seq      INTEGER NOT NULL,
  superseded_at    TEXT NOT NULL,
  superseded_by_run_id TEXT,
  source_system    TEXT,
  filing_id        TEXT,
  value_text       TEXT,
  value_num        REAL,
  version_status   TEXT,
  validation       TEXT,
  qa_flags         TEXT,
  review_status    TEXT,
  row_json         TEXT NOT NULL,
  PRIMARY KEY (observation_id, version_seq)
);
CREATE INDEX IF NOT EXISTS ix_obsver_filing
  ON observation_versions(source_system, filing_id);

-- =========================================================================
-- SET-BASED LINEAGE                                             (audit A08)
-- =========================================================================
-- The 8 September audit found 1,870 eLibrary edges with neither an input fact
-- nor an input observation, 313 IOC MDQ and 158 storage totals with no edges at
-- all, contract aggregates anchored only to another aggregate, and 52 derived
-- 549D zero-numerator results with nothing behind them.
--
-- The rule it established: every contributing input must be traversable, either
-- through occurrence-specific edges OR through an explicitly persisted,
-- independently verifiable source-row population with its inclusion and
-- exclusion rules recorded. A pointer to an unrelated aggregate is not lineage.
--
-- A ZERO MUST HAVE A SUPPORTED POPULATION. "We retrieved nothing" is not zero,
-- and the difference is exactly whether a row exists here saying which rows were
-- considered and why none qualified.
CREATE TABLE IF NOT EXISTS lineage_populations (
  population_id    TEXT PRIMARY KEY,
  observation_id   TEXT NOT NULL REFERENCES observations(observation_id) ON DELETE CASCADE,
  -- how the set was drawn: the exact source table and the filing occurrences it
  -- was drawn from, so a reviewer can redraw it independently
  source_system    TEXT NOT NULL,
  source_table     TEXT NOT NULL,            -- source_facts | ioc_contract_rows | d549_rows | ...
  filing_ids       TEXT NOT NULL,            -- JSON array of filing occurrences
  -- the rules, stated positively and negatively. Both are required: a set
  -- defined only by what it includes cannot be checked for what it wrongly kept.
  inclusion_rule   TEXT NOT NULL,
  exclusion_rule   TEXT NOT NULL,
  -- the resulting set, so the claim is checkable without re-running the engine
  row_count        INTEGER NOT NULL,         -- rows that MATCHED (may legitimately be 0)
  candidate_count  INTEGER NOT NULL,         -- rows considered before exclusions
  excluded_count   INTEGER NOT NULL,
  member_key       TEXT NOT NULL,            -- which column identifies a member
  member_digest    TEXT NOT NULL,            -- sha256 over the sorted member keys
  members_sample   TEXT,                     -- JSON array, first N member keys
  aggregate_value  TEXT,                     -- the value this population produced
  aggregate_unit   TEXT,
  -- a zero result must say WHY the set is empty, and empty-because-nothing-was-
  -- retrieved is a different fact from empty-because-nothing-qualified
  empty_reason     TEXT,
  note             TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lineage_pop_obs ON lineage_populations(observation_id);

-- A document-sourced assertion. Every one carries its exact span and evidence:
-- a Form 2-A narrative "approximately 185 miles" is a real FERC disclosure, but
-- it is not a p.514 certified total and must never be labelled as one.
CREATE TABLE IF NOT EXISTS document_facts (
  document_fact_id TEXT PRIMARY KEY,
  document_id      TEXT REFERENCES documents(document_id) ON DELETE CASCADE,
  source_system    TEXT,
  filing_id        TEXT,
  source_fact_id   TEXT,                      -- when the span came from an XBRL textblock
  entity_key       TEXT NOT NULL,
  assertion_type   TEXT NOT NULL,
  metric_id        TEXT,
  value_text       TEXT,
  value_num        REAL,
  unit             TEXT,
  qualifier        TEXT,                      -- "approximately", "combined", ...
  scope_note       TEXT NOT NULL,             -- what the figure actually covers
  page             TEXT,
  paragraph        TEXT,
  char_start       INTEGER,
  char_end         INTEGER,
  verbatim_span    TEXT NOT NULL,
  extraction_method TEXT NOT NULL,            -- xbrl_textblock_regex | pdf_text_span | manual_review
  content_hash     TEXT,
  confidence       TEXT,
  review_state     TEXT,                      -- '' | queued | reviewed | rejected
  reviewer_note    TEXT,
  first_seen_at    TEXT
);

CREATE INDEX IF NOT EXISTS ix_docfacts_entity ON document_facts(entity_key, assertion_type);

CREATE TABLE IF NOT EXISTS events (
  event_id       TEXT PRIMARY KEY,
  entity_key     TEXT,
  asset_ids      TEXT,                        -- JSON array: one event, many assets
  event_class    TEXT NOT NULL,               -- rate | authority | operational | revision | data_quality
  event_type     TEXT NOT NULL,
  headline       TEXT NOT NULL,
  detail         TEXT,
  destination    TEXT NOT NULL,               -- investor_feed | filing_archive | data_review_queue
  source_system  TEXT,
  filing_id      TEXT,
  accession_number TEXT,
  document_id    TEXT,
  docket         TEXT,
  reporting_date TEXT,
  source_filed_date TEXT,
  source_posted_date TEXT,
  effective_date TEXT,
  first_seen_at  TEXT NOT NULL,
  is_backfill    INTEGER NOT NULL DEFAULT 0,  -- history must not flood the current feed
  comparison_basis TEXT,
  confidence_note TEXT
);

-- ---------------------------------------------------------------- coverage

-- Frozen BEFORE the run. The denominator is what the template requires, never
-- what the run happened to produce.
CREATE TABLE IF NOT EXISTS coverage_expected (
  slot_id          TEXT PRIMARY KEY,
  entity_key       TEXT NOT NULL,
  asset_id         TEXT,
  template         TEXT NOT NULL,
  metric_id        TEXT NOT NULL,
  source_regime    TEXT NOT NULL,
  period_basis     TEXT NOT NULL,
  period_start     TEXT,
  period_end       TEXT,
  instant_date     TEXT,
  reporting_year   INTEGER,
  reporting_period TEXT,
  scope            TEXT NOT NULL,
  unit_rule        TEXT,
  requirement      TEXT NOT NULL,             -- REQUIRED | CONDITIONALLY_REQUIRED |
                                              -- OPTIONAL_OR_SUPPLEMENTAL | NOT_REQUIRED |
                                              -- APPLICABILITY_UNKNOWN
  requirement_evidence TEXT,
  applicability_version TEXT,
  frozen_at        TEXT NOT NULL,
  frozen_run_id    TEXT,

  -- ------------------------------------------------------------------ A05
  -- The obligation calendar. Before these columns the denominator was built
  -- from `[f for f in filings if f["is_canonical"]]`, so a slot existed only
  -- where a fetch AND a parse had succeeded: a broken parser deleted its own
  -- obligations and coverage went UP. Northern Border's 11 IOC filings, every
  -- one failing on a BOM before the required H record, contributed 8 undated
  -- unknowns instead of 96 dated obligations.
  --
  -- `source_health` is the load-bearing one. Without it a slot that exists
  -- BECAUSE a parser failed is indistinguishable from a slot nobody attempted,
  -- and that distinction is the whole of A05.
  due_date         TEXT,                      -- the official 18 CFR deadline
  slot_state       TEXT,                      -- future_not_yet_due | due_and_open |
                                              -- overdue | applicability_unknown |
                                              -- technical_failure | outside_declared_window
  obligation_form  TEXT,
  obligation_authority TEXT,                  -- the regulation the duty comes from
  obligation_evidence_kind TEXT,
  source_health    TEXT,                      -- ok | not_indexed | index_failed |
                                              -- retrieval_failed | parse_failed | not_retrieved
  source_health_detail TEXT,
  denominator_origin TEXT,                    -- calendar | shipped_grid | roster
  docket           TEXT,
  facility         TEXT,
  shipped_requirement TEXT                    -- what the pre-repair grid said, kept for the bridge
);

CREATE INDEX IF NOT EXISTS ix_expected_entity ON coverage_expected(entity_key, metric_id);
CREATE INDEX IF NOT EXISTS ix_expected_state ON coverage_expected(slot_state, source_health);

-- Measured after the run. populated / source_matched / validated / review are
-- reported separately; none of them is allowed to be improved by deletion.
CREATE TABLE IF NOT EXISTS coverage_measured (
  slot_id        TEXT PRIMARY KEY REFERENCES coverage_expected(slot_id) ON DELETE CASCADE,
  run_id         TEXT,
  observation_id TEXT,
  outcome        TEXT NOT NULL,               -- see ferclib.status.CoverageOutcome
  populated      INTEGER NOT NULL DEFAULT 0,
  source_matched INTEGER NOT NULL DEFAULT 0,
  validated      INTEGER NOT NULL DEFAULT 0,
  in_review      INTEGER NOT NULL DEFAULT 0,
  reason         TEXT,
  evidence       TEXT,
  measured_at    TEXT,
  -- A06. Matching now refuses a candidate on unit family, period basis, scope
  -- or filing version. A refusal is not the same as an absence, and a slot that
  -- had a value we declined must say so and say WHY -- otherwise "no match"
  -- silently covers both "nothing was produced" and "something was produced and
  -- we would not accept it", which are opposite engineering problems.
  candidates_refused INTEGER NOT NULL DEFAULT 0,
  refusal_gates  TEXT                         -- which gate rejected each candidate
);

-- Every requested template field ends in a measured implementation status.
-- A placeholder column or a function returning GATED is NOT implemented.
CREATE TABLE IF NOT EXISTS field_status (
  template       TEXT NOT NULL,
  field_id       TEXT NOT NULL,
  metric_id      TEXT,
  adapter        TEXT,
  implementation TEXT NOT NULL,               -- implemented | not_implemented | partial
  outcome        TEXT NOT NULL,               -- see ferclib.status.FieldOutcome
  evidence       TEXT,
  blocker        TEXT,
  note           TEXT,

  -- ------------------------------------------------------------------ A07
  -- Readiness measured WITHIN the field's own template. `build_field_status`
  -- used to query by metric_id with no template predicate and write the same
  -- verdict onto every template that requested the metric, so a gas metric
  -- passing in interstate gas marked the same field validated in gas storage.
  -- Seven of the 144 "implemented, retrieved, validated" rows had no
  -- present/pass observation in their own template and four had no present
  -- observation at all.
  --
  -- The three flags are deliberately SEPARATE, because they are three different
  -- claims: that we built the adapter, that data arrived here, and that what
  -- arrived passed. Collapsing them is what let one stand for another.
  adapter_implemented        INTEGER,
  has_data_in_template       INTEGER,
  validated_in_template      INTEGER,
  -- the counts the verdict was computed from, so it can be re-derived
  within_template_observations   INTEGER,
  within_template_present        INTEGER,
  within_template_present_pass   INTEGER,
  within_template_entities       INTEGER,
  within_template_regimes        TEXT,
  selector_failures_in_template  INTEGER,
  core_slots_in_template         INTEGER,
  -- kept ONLY so a cross-template pass is visible as a fact rather than
  -- silently credited to this template. It must never drive the verdict.
  cross_template_present_pass    INTEGER,
  PRIMARY KEY (template, field_id)
);

-- Requirements crosswalk: every Day 2 / Day 3 row mapped to a revised metric ID
-- so no hard metric is silently discarded.
CREATE TABLE IF NOT EXISTS requirements_crosswalk (
  source_doc      TEXT NOT NULL,
  source_row      TEXT NOT NULL,
  disposition     TEXT NOT NULL,              -- carried | renamed | merged | gated | superseded | new
  metric_id       TEXT,
  template        TEXT,
  note            TEXT,
  PRIMARY KEY (source_doc, source_row)
);

-- ---------------------------------------------------------------- versions

CREATE TABLE IF NOT EXISTS applicability (
  form              TEXT NOT NULL,
  taxonomy_version  TEXT NOT NULL,
  concept_local     TEXT NOT NULL,
  in_form           TEXT NOT NULL,            -- yes | no | unknown
  schedule_page     TEXT,
  schedule_name     TEXT,
  conditionality    TEXT,                     -- unconditional | conditional | unknown
  evidence          TEXT NOT NULL,
  resolved_at       TEXT,
  PRIMARY KEY (form, taxonomy_version, concept_local)
);

CREATE TABLE IF NOT EXISTS taxonomy_sources (
  form             TEXT NOT NULL,
  taxonomy_version TEXT NOT NULL,
  artefact         TEXT NOT NULL,             -- entry_point | presentation_linkbase | label_linkbase
  url              TEXT NOT NULL,
  content_hash     TEXT,
  retrieved        INTEGER NOT NULL DEFAULT 0,
  note             TEXT,
  PRIMARY KEY (form, taxonomy_version, artefact, url)
);

CREATE TABLE IF NOT EXISTS blockers (
  blocker_id   TEXT PRIMARY KEY,
  adapter      TEXT NOT NULL,
  scope        TEXT,
  kind         TEXT NOT NULL,                 -- access | source | semantic | unimplemented | environment
  summary      TEXT NOT NULL,
  attempts     TEXT,
  exact_error  TEXT,
  human_decision_needed INTEGER NOT NULL DEFAULT 0,
  opened_at    TEXT NOT NULL,
  resolved_at  TEXT
);

-- Reviewed conclusions about ONE exact source observation. Data, never code.
-- An annotation may annotate but never change a filed value, and applies only
-- when every key AND the filed text still match.
CREATE TABLE IF NOT EXISTS reviewed_source_annotations (
  source_system  TEXT NOT NULL,
  entity_key     TEXT NOT NULL,
  filing_id      TEXT NOT NULL,
  source_fact_id TEXT NOT NULL,
  metric_id      TEXT NOT NULL,
  filed_text     TEXT NOT NULL,
  review_status  TEXT NOT NULL,
  rationale      TEXT NOT NULL,
  evidence_ref   TEXT,
  evidence_hash  TEXT,
  reviewer       TEXT,
  reviewed_at    TEXT,
  applied_count  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source_system, entity_key, filing_id, source_fact_id, metric_id)
);
