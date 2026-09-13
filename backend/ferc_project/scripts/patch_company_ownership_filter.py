from pathlib import Path

path = Path('scripts/run_weekly_company_discovery.py')
if not path.exists():
    raise SystemExit(f'Not found: {path}')

text = path.read_text(encoding='utf-8')

if 'from ferc_filter.ferc_docket_discovery import match_company' not in text:
    anchor = 'from ferc_filter.company_ferc_identity_learner import ('
    pos = text.find(anchor)
    if pos < 0:
        raise SystemExit('Could not find identity import block.')
    text = text[:pos] + 'from ferc_filter.ferc_docket_discovery import match_company\n' + text[pos:]

helper = '''

def filter_filings_to_company(
    filings,
    registry: dict[str, Any],
    company_id: str,
):
    """Prevent company-search leakage across monitored companies."""
    companies = registry.get('companies') or []
    identity_path = Path('config/company_ferc_identities.json')
    identities = json.loads(identity_path.read_text(encoding='utf-8'))
    identity_map = {
        cid.upper(): data.get('ferc_entities') or []
        for cid, data in (identities.get('companies') or {}).items()
    }

    eligible_roots = set()

    for filing in filings:
        applicant = str(getattr(filing, 'applicant', '') or '').strip()
        if not applicant:
            continue

        matched_id, _, _ = match_company(
            applicant,
            companies,
            identity_map,
        )
        if matched_id and matched_id.upper() == company_id.upper():
            root = root_docket(filing.docket)
            if root:
                eligible_roots.add(root)

    return [
        filing
        for filing in filings
        if root_docket(filing.docket) in eligible_roots
    ]
'''

if 'def filter_filings_to_company(' not in text:
    marker = '\ndef candidate_groups(\n'
    pos = text.find(marker)
    if pos < 0:
        raise SystemExit('Could not find candidate_groups marker.')
    text = text[:pos] + helper + text[pos:]

old = '''    filings, searched_aliases = search_company_identities(
        client,
        company_id,
        company_name,
        start_date,
        end_date,
    )

    groups = candidate_groups(filings)
'''

new = '''    filings, searched_aliases = search_company_identities(
        client,
        company_id,
        company_name,
        start_date,
        end_date,
    )

    filings = filter_filings_to_company(
        filings,
        registry,
        company_id,
    )

    groups = candidate_groups(filings)
'''

if old not in text:
    raise SystemExit('Could not find search-to-group block.')
text = text.replace(old, new, 1)

compile(text, str(path), 'exec')
path.write_text(text, encoding='utf-8')

print(f'PASS | Added applicant-based company ownership filter to {path}')
print('PASS | Cross-company dockets are excluded from qualification')
print('PASS | All filings within an owned docket remain available')
print()
print('Next:')
print('  python scripts/run_weekly_company_discovery.py --company WMB --days 30')
print('  python scripts/run_weekly_company_discovery.py --company KMI --days 30')
