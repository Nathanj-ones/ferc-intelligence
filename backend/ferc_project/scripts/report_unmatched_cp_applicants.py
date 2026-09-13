from __future__ import annotations
import argparse, csv, json
from pathlib import Path
from ferc_filter.unmatched_cp_applicants import collect_unmatched_applicants

def load_json(path: Path): return json.loads(path.read_text(encoding="utf-8"))

def main() -> None:
    parser = argparse.ArgumentParser(description="Report unresolved operating applicants from the recent FERC CP feed.")
    parser.add_argument("--feed", default="data/discovery/recent_cp_feed.json")
    parser.add_argument("--registry", default="config/project_registry.json")
    parser.add_argument("--identities", default="config/company_ferc_identities.json")
    parser.add_argument("--ownership", default="config/company_ownership_registry.json")
    parser.add_argument("--out", default="data/discovery/unmatched_cp_applicants.json")
    parser.add_argument("--csv-out", default="data/discovery/unmatched_cp_applicants.csv")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    feed, registry, identities = load_json(Path(args.feed)), load_json(Path(args.registry)), load_json(Path(args.identities))
    ownership_path = Path(args.ownership)
    ownership = load_json(ownership_path) if ownership_path.exists() else {"entities": []}
    records = feed.get("records", []) if isinstance(feed, dict) else feed
    companies = registry.get("companies", [])
    identity_map = {cid.upper(): list((payload or {}).get("ferc_entities") or []) for cid, payload in (identities.get("companies") or {}).items()}
    results = collect_unmatched_applicants(records, companies, identity_map, ownership)
    json_path, csv_path = Path(args.out), Path(args.csv_out)
    json_path.parent.mkdir(parents=True, exist_ok=True); csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps({"schema_version":"unmatched_cp_applicants_v0.1","source_feed":str(args.feed),"candidate_count":len(results),"candidates":[x.to_dict() for x in results]}, indent=2)+"\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer=csv.writer(handle); writer.writerow(["applicant","docket_count","filing_count","initiating_filings","regulatory_filings","construction_filings","compliance_filings","dockets","latest_filed_date","sample_description"])
        for x in results: writer.writerow([x.applicant,x.docket_count,x.filing_count,x.initiating_filings,x.regulatory_filings,x.construction_filings,x.compliance_filings,"; ".join(x.dockets),x.latest_filed_date,x.sample_description])
    print("="*100); print("UNMATCHED RECENT CP APPLICANTS v0.1"); print("="*100)
    print(f"Recent CP records: {len(records)}"); print(f"Unmatched plausible project applicants: {len(results)}\n")
    print(f"{'Applicant':55} {'Dockets':>7} {'Filings':>7} {'Init':>5} {'Reg':>5}"); print("-"*100)
    for x in results[:max(args.limit,0)]: print(f"{x.applicant[:55]:55} {x.docket_count:7d} {x.filing_count:7d} {x.initiating_filings:5d} {x.regulatory_filings:5d}")
    print(f"\nSaved JSON: {json_path}\nSaved CSV:  {csv_path}")
if __name__ == "__main__": main()
