from pathlib import Path

path = Path("scripts/run_weekly_company_discovery.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

old = '    print(f"Already tracked: {result[\'existing_tracked\']}")\n'
new = (
    '    registry = load_registry(args.registry)\n'
    '    total_tracked = len(tracked_roots(registry, args.company.upper()))\n'
    '    print(f"Tracked projects in registry: {total_tracked}")\n'
    '    print("Tracked dockets seen in discovery window:", '
    'result["existing_tracked"])\n'
)

if old not in text:
    raise SystemExit("Could not find Already tracked output line.")

text = text.replace(old, new, 1)
compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")
print(f"PASS | Updated summary labels in {path}")
