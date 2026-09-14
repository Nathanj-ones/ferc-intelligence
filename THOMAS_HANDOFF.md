# FERC Intelligence Project — Thomas Handoff

## Executive summary

This repository contains a testable FERC project-intelligence workflow for six companies: Kinder Morgan (KMI), Targa Resources (TRGP), Williams (WMB), ONEOK (OKE), Cheniere Energy (LNG), and NextDecade (NEXT).

The system discovers recent FERC CP-docket activity, resolves filings to monitored companies using controlled legal-entity and ownership mappings, qualifies potential projects, applies a materiality gate, promotes qualifying TRACK projects into a project registry, monitors tracked dockets, generates ProjectView records, and exports those records to the frontend dashboard.

The current build is suitable for testing and demonstration. It is intentionally conservative: missing economic information is not invented, unknown applicants are not guessed into companies, and a company can be actively monitored by discovery while having no projects currently meeting the TRACK threshold.

## Current validated state

The final production validation completed successfully with:

- 6 companies attempted
- 3 companies with tracked projects monitored
- 3 companies cleanly skipped because they currently have no enabled TRACK projects
- 0 new projects promoted on the final idempotency run
- 0 warnings
- 0 failures

The dashboard bridge currently exports 9 tracked projects with real company mappings.

A useful end-to-end example is Williams' Leidy Access Expansion Project (CP26-576). It was discovered through the project-discovery workflow, correctly resolved to Williams, promoted into the registry, monitored successfully, converted into a ProjectView, and exported to the dashboard.

## Current company coverage

| Company | Current state |
| --- | --- |
| Kinder Morgan | 3 tracked projects currently represented in the dashboard |
| Targa Resources | Included in the six-company discovery universe, but current discovery coverage remains limited and no projects are presently TRACK |
| Williams | 4 tracked projects, including auto-discovered CP26-576 |
| ONEOK | Included in discovery; evidence-backed ownership resolution expands coverage, but current evaluated candidates do not meet the materiality threshold for TRACK |
| Cheniere Energy | Included in discovery and producing candidate activity, but no current candidate meets the TRACK threshold |
| NextDecade | 2 tracked projects currently represented in the dashboard |

“No TRACK projects” does not mean that a company has no real-world projects. It means that, under the current evidence, qualification and materiality rules, no additional project has met the system's threshold for promotion into the monitored-project registry.

## Pipeline

```text
Recent FERC activity
        ↓
Forward company search + reverse recent-CP discovery
        ↓
Applicant / legal-entity ownership validation
        ↓
Docket grouping and project-name extraction
        ↓
Candidate qualification
        ↓
Evidence extraction
        ↓
Materiality evaluation
        ↓
TRACK / WATCH / DROP
        ↓
TRACK-only registry promotion
        ↓
Project monitor
        ↓
ProjectView
        ↓
Dashboard export
```

## Ownership safety

Company assignment is deliberately conservative. The system uses known FERC legal identities plus an evidence-backed ownership registry. Unknown applicants remain unresolved instead of being assigned using loose fuzzy-name guesses.

Regression tests protect important ownership boundaries, including Transcontinental Gas Pipe Line Company → Williams only; Tennessee Gas Pipeline Company → Kinder Morgan only; Rio Grande LNG → NextDecade only; unknown applicant → no company; and CP26-576 → Williams only.

ONEOK coverage also includes evidence-backed ownership metadata for additional legal entities, including the Northern Border joint-venture relationship.

## TRACK, WATCH and DROP

**TRACK** means the project has sufficient qualification and materiality evidence to enter the monitored-project registry.

**WATCH** means the activity is plausibly project-relevant but does not currently have enough evidence to justify automatic TRACK promotion.

**DROP** means the candidate does not currently meet the materiality/relevance criteria.

Only TRACK candidates are automatically promoted. WATCH candidates are deliberately not promoted. Promotion is idempotent: rerunning the workflow does not create duplicate registry projects.

## Running the weekly workflow

From the repository root:

```powershell
cd backend\ferc_project
$env:PYTHONPATH="$PWD\src"
```

Dry run:

```powershell
python scripts\run_all_company_weekly.py --days 30
```

Apply and monitor:

```powershell
python scripts\run_all_company_weekly.py --days 30 --apply
```

Companies with no enabled tracked projects are treated as normal skips rather than failures.

## Rebuilding the dashboard

Return to the repository root:

```powershell
cd ..\..
python backend\build_project_dashboard.py
```

The normal repository command is:

```powershell
npm run build
```

On the Windows validation machine, the repository's existing `prebuild` backend-snapshot integrity check reported a one-byte `directory.json` size mismatch on a clean Git checkout. Git reported the snapshot files themselves as unmodified. The actual application build was validated directly with:

```powershell
npx vinext build
```

That build completed successfully.

For local development:

```powershell
npm run dev
```

## Important regression tests

From `backend\ferc_project` with `PYTHONPATH` set to `src`:

```powershell
python scripts\test_weekly_discovery_loop.py
python scripts\test_company_ownership_registry.py
python scripts\test_unmatched_cp_applicants.py
python scripts\test_reverse_cp_weekly_integration.py
python scripts\test_company_ownership_regression.py
python scripts\test_discovery_monitor_integration.py
python scripts\test_all_company_weekly.py
```

For the dashboard bridge, from the repository root:

```powershell
python backend\test_project_dashboard_bridge.py
```

The final handoff validation passed all of the above. The dashboard bridge reported 9 exported projects, real company mappings for every project, and the required ProjectView contract fields.

## Key locations

```text
backend/ferc_project/config/       Company identity, ownership and project registry configuration
backend/ferc_project/src/ferc_filter/  Core discovery, qualification, materiality, ownership and monitoring logic
backend/ferc_project/scripts/      Weekly orchestration, monitor commands, diagnostics and tests
backend/build_project_dashboard.py Bridge from backend ProjectViews to frontend project data
lib/ferc/projects-live.json        Current dashboard project dataset
app/, components/ferc/, lib/ferc/ Frontend/dashboard integration
```

Runtime data under `backend/ferc_project/data/`, local `.env` files, databases, Python caches and other generated artifacts are intentionally excluded from Git.

## Known limitations

### Targa coverage

Targa is currently the weakest discovery-coverage case. The system is configured to monitor Targa, but the current FERC identity/ownership universe has not produced owned CP dockets in the recent discovery window. This should be described as a coverage limitation rather than as evidence that Targa has no projects.

### ONEOK and Cheniere

These companies are included in discovery even though they currently have no TRACK projects. ONEOK's reverse-discovery coverage has been expanded using evidence-backed ownership relationships. Recent ONEOK candidates were successfully discovered and qualified but did not pass the existing materiality threshold.

Cheniere also produces discovery candidates, but none currently meet the TRACK threshold. Thresholds were deliberately not weakened simply to populate the dashboard.

### FERC availability

FERC endpoints can intermittently return timeouts or 520-class errors. The monitor contains retry/defer behavior so transient source availability does not require inventing data or treating every source outage as a project-level failure.

### Economic evidence

Capex, capacity and target in-service dates are only populated when supported by available evidence. Missing values remain missing rather than being inferred.

### Coverage is not claimed to be exhaustive

The product is designed as a conservative, testable intelligence workflow. It does not claim perfect coverage of every FERC affiliate, docket or corporate ownership relationship.

## Suggested test/demo flow

1. Open the dashboard and show the current tracked-project universe.
2. Show Williams and CP26-576 as the end-to-end discovery example.
3. Explain that all six companies participate in discovery even though only some currently have TRACK projects.
4. Run the all-company workflow in dry-run mode.
5. Show the TRACK/WATCH/DROP output and explain that only sufficiently supported TRACK candidates are promoted.
6. If required, rebuild the dashboard data and refresh the frontend.

## Recommended next steps after testing

These are future improvements rather than requirements for the current testable release:

- expand evidence-backed corporate ownership coverage where justified;
- improve Targa discovery coverage;
- review WATCH candidates over time as new FERC evidence arrives;
- improve human-readable presentation of internal stage labels;
- resolve the existing Windows backend-snapshot prebuild byte-integrity discrepancy;
- add deployment/scheduling only after the current workflow has been reviewed in testing.

## Handoff status

The code was committed and pushed to the repository `main` branch after final regression testing and frontend build validation.

The handoff baseline is the current `main` branch with passing GitHub CI.

The system should now be treated as a testable baseline. Further changes should be driven by observed test results or confirmed coverage gaps rather than by lowering evidence thresholds to increase project counts.
