from pathlib import Path
import re

TARGETS = [
    Path("scripts/test/real/project_view.py"),
    Path("scripts/test/_real/project_view.py"),
    Path("scripts/test/real/test_real_project_view.py"),
    Path("scripts/test/_real/test_real_project_view.py"),
    Path("scripts/test_real_project_view.py"),
    Path("scripts/test/_real_pending/regulatory_action.py"),
    Path("scripts/test/real/pending/regulatory_action.py"),
    Path("scripts/test/real/test_real_pending_regulatory_action.py"),
    Path("scripts/test/_real/test_real_pending_regulatory_action.py"),
    Path("scripts/test_real_pending_regulatory_action.py"),
    Path("scripts/test/_real_pending_regulatory_action.py"),
]

PROJECT_LINES = [
    '    "CP25-10": {',
    '        "name": "Southeast Supply Enhancement",',
    '    },',
    '    "CP25-528": {',
    '        "name": "Appalachian Reliability",',
    '    },',
    '    "CP25-219": {',
    '        "name": "Southeast Compression Utility and Reliability",',
    '    },',
    '    "CP21-465": {',
    '        "name": "Driftwood Line 200",',
    '    },',
    '    "CP17-101": {',
    '        "name": "Northeast Supply Enhancement",',
    '    },',
]

def patch_file(path: Path) -> bool:
    if not path.exists():
        return False

    text = path.read_text(encoding="utf-8")

    if '"CP21-465"' in text and '"CP17-101"' in text:
        print(f"PASS | Already updated: {path}")
        return True

    match = re.search(
        r'PROJECTS\s*=\s*\{.*?\n\}',
        text,
        flags=re.DOTALL,
    )
    if not match:
        print(f"SKIP | No PROJECTS dictionary found: {path}")
        return False

    start = match.start()
    end = match.end()
    old_block = match.group(0)

    # Preserve indentation/style while replacing only the allowlist.
    new_block = "PROJECTS = {\n" + "\n".join(PROJECT_LINES) + "\n}"

    text = text[:start] + new_block + text[end:]
    path.write_text(text, encoding="utf-8")
    compile(text, str(path), "exec")
    print(f"PASS | Updated: {path}")
    return True

found = False
for target in TARGETS:
    if patch_file(target):
        found = True

if not found:
    print("ERROR | No matching real-project test scripts were found.")
    print("Expected files include:")
    print("  scripts/test_real_project_view.py")
    print("  scripts/test_real_pending_regulatory_action.py")
    raise SystemExit(1)

print()
print("Real-project test allowlists now include:")
print("  CP25-10   Southeast Supply Enhancement")
print("  CP25-528  Appalachian Reliability")
print("  CP25-219  Southeast Compression Utility and Reliability")
print("  CP21-465  Driftwood Line 200")
print("  CP17-101  Northeast Supply Enhancement")
