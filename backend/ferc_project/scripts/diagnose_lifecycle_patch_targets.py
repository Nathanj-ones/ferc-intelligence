from pathlib import Path
import re

roots = [
    Path("scripts"),
    Path("src"),
]

patterns = [
    re.compile(r"\blifecycle\b"),
    re.compile(r"\bproject_lifecycle\b"),
    re.compile(r"build_project_lifecycle"),
    re.compile(r"find_pending_regulatory_actions"),
    re.compile(r"pending_actions"),
]

found = []

for root in roots:
    if not root.exists():
        continue

    for path in root.rglob("*.py"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        hits = []
        for number, line in enumerate(lines, start=1):
            if any(pattern.search(line) for pattern in patterns):
                hits.append((number, line))

        if hits:
            found.append((path, hits))

print("=" * 90)
print("PROJECT LIFECYCLE PATCH DIAGNOSTIC")
print("=" * 90)

for path, hits in found:
    print()
    print(path)
    print("-" * 90)
    for number, line in hits[:80]:
        print(f"{number:5}: {line}")

print()
print("Files containing lifecycle/pending-action references:", len(found))

if not found:
    print("No matching references found.")
    raise SystemExit(1)

print()
print("Send this output back so the integration can be patched against the")
print("actual local file structure instead of assuming stale formatting.")
