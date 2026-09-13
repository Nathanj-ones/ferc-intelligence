from dataclasses import dataclass
from ferc_filter.company_project_discovery import root_docket


assert root_docket("CP26-571-000") == "CP26-571"
assert root_docket("CP25-10-001") == "CP25-10"
assert root_docket("ER26-100") is None
print("PASS | CP root normalization works for reverse discovery")

print()
print("ALL RECENT FERC CP FEED v0.2 TESTS PASSED")
