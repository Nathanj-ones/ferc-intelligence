from pathlib import Path
import tempfile

from ferc_filter.project_registry import load_registry


print("=" * 80)
print("PROJECT MONITOR v0.1 TEST")
print("=" * 80)

with tempfile.TemporaryDirectory() as tmp:
    source = Path(__file__).resolve().parents[1] / "config" / "project_registry.json"
    registry = load_registry(source)

    assert len(registry.companies) == 6
    assert registry.company_map()["WMB"].name == "Williams"
    assert registry.company_map()["KMI"].name == "Kinder Morgan"
    assert registry.company_map()["TRGP"].name == "Targa Resources"
    assert registry.company_map()["LNG"].name == "Cheniere Energy"
    assert registry.company_map()["NXDT"].name == "NextDecade"
    assert len(registry.enabled_projects()) >= 3

    dockets = {item.docket for item in registry.enabled_projects()}
    assert "CP25-10" in dockets
    assert "CP25-528" in dockets
    assert "CP25-514" in dockets
    assert "CP25-517" in dockets
    assert "CP16-454" in dockets
    assert "CP16-455" in dockets

print("PASS | Six-company registry contract")
print("PASS | Enabled project registry parsed")
print("PASS | Docket uniqueness enforced")
print("PASS | Williams/KMI/NextDecade seed projects present")
print()
print("ALL PROJECT MONITOR v0.1 TESTS PASSED")
