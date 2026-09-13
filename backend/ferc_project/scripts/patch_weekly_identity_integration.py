from pathlib import Path

path = Path("scripts/run_weekly_company_discovery.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

# Add identity-learning imports.
anchor = "from ferc_filter.discovery_registry_pipeline import ("
imports = (
    "from ferc_filter.company_ferc_identity_learner import (\n"
    "    learn_entities,\n"
    "    merge_learned_entities,\n"
    ")\n"
    "from ferc_filter.company_multi_entity_search import (\n"
    "    search_company_identities,\n"
    ")\n"
)

if "company_ferc_identity_learner" not in text:
    pos = text.find(anchor)
    if pos < 0:
        raise SystemExit("Could not find import insertion anchor.")
    text = text[:pos] + imports + text[pos:]
    print("PASS | Added identity learner/search imports")
else:
    print("PASS | Identity imports already present")

# Replace the single parent-name search in run_company().
old = '''    client = FERCDiscoveryClient()
    filings = client.search_company(
        company_name=company_name,
        start_date=start_date,
        end_date=end_date,
    )

    groups = candidate_groups(filings)
'''

new = '''    client = FERCDiscoveryClient()

    # Bootstrap from the parent-company search.
    parent_filings = client.search_company(
        company_name=company_name,
        start_date=start_date,
        end_date=end_date,
    )

    # Learn recurring FERC-facing legal entities from those filings.
    learned = learn_entities(
        parent_filings,
        minimum_filings=2,
    )
    identity_path = Path("config/company_ferc_identities.json")
    learned_added = merge_learned_entities(
        company_id,
        learned,
        path=identity_path,
    )

    # Search parent + all persisted identities and deduplicate the result.
    filings, searched_aliases = search_company_identities(
        client,
        company_id,
        company_name,
        start_date,
        end_date,
    )

    groups = candidate_groups(filings)
'''

if old not in text:
    raise SystemExit(
        "Could not find parent-company search block. "
        "No changes written."
    )
text = text.replace(old, new, 1)

# Add identity information to saved result.
old_result = '''        "filings_scanned": len(filings),
        "dockets_seen": len(groups),
'''

new_result = '''        "filings_scanned": len(filings),
        "dockets_seen": len(groups),
        "ferc_identities_searched": searched_aliases,
        "ferc_identities_learned": learned_added,
'''

if old_result not in text:
    raise SystemExit("Could not find result summary block.")
text = text.replace(old_result, new_result, 1)

# Add useful console output.
old_print = '''    print(f"Filings scanned: {result['filings_scanned']}")
    print(f"Dockets seen: {result['dockets_seen']}")
'''

new_print = '''    print(
        "FERC identities searched:",
        len(result["ferc_identities_searched"]),
    )
    for identity in result["ferc_identities_searched"]:
        print("  -", identity)
    if result["ferc_identities_learned"]:
        print(
            "New identities learned:",
            len(result["ferc_identities_learned"]),
        )
        for identity in result["ferc_identities_learned"]:
            print("  +", identity)
    print(f"Filings scanned: {result['filings_scanned']}")
    print(f"Dockets seen: {result['dockets_seen']}")
'''

if old_print not in text:
    raise SystemExit("Could not find console summary block.")
text = text.replace(old_print, new_print, 1)

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

print()
print(f"PASS | Integrated identity learning into {path}")
print()
print("Next:")
print("  python scripts/run_weekly_company_discovery.py --company TRGP --days 30")
