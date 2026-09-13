from pathlib import Path

path = Path("scripts/test_real_pending_regulatory_action.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

if "action.explicit_deadline" in text:
    text = text.replace("action.explicit_deadline", "action.requested_timing")
    text = text.replace('"Explicit deadline:"', '"Requested timing:"')
    print("PASS | Replaced stale explicit_deadline references")
else:
    print("PASS | No stale explicit_deadline reference found")

path.write_text(text, encoding="utf-8")
compile(text, str(path), "exec")
print(f"PASS | Updated {path}")
