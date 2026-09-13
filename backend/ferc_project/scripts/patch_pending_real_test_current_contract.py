from pathlib import Path
import re

path = Path("scripts/test_real_pending_regulatory_action.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

replacements = {
    "action.explicit_deadline": "action.requested_timing",
    "action.deadline_source": "action.timing_source",
    '"Explicit deadline:"': '"Requested timing:"',
    '"Deadline source:"': '"Timing source:"',
}

changed = []
for old, new in replacements.items():
    if old in text:
        text = text.replace(old, new)
        changed.append(f"{old} -> {new}")

# Guard against duplicate timing output introduced by earlier one-off patches.
# This only collapses an exact duplicated adjacent print block if present.
duplicate = '''        if action.requested_timing:
            print(
                "Requested timing:",
                action.requested_timing.strftime("%Y-%m-%d"),
            )
            print(
                "Timing source:",
                action.timing_source,
            )
        if action.requested_timing:
            print(
                "Requested timing:",
                action.requested_timing.strftime("%Y-%m-%d"),
            )
            print(
                "Timing source:",
                action.timing_source,
            )
'''
single = '''        if action.requested_timing:
            print(
                "Requested timing:",
                action.requested_timing.strftime("%Y-%m-%d"),
            )
            print(
                "Timing source:",
                action.timing_source,
            )
'''
if duplicate in text:
    text = text.replace(duplicate, single)
    changed.append("collapsed duplicate requested-timing print block")

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

print(f"PASS | Updated {path}")
if changed:
    for item in changed:
        print(f"  {item}")
else:
    print("  No stale PendingRegulatoryAction field names remained.")

stale = [
    name for name in (
        "explicit_deadline",
        "deadline_source",
    )
    if f"action.{name}" in text
]

if stale:
    print("WARNING | Stale fields still present:", ", ".join(stale))
    raise SystemExit(1)

print("PASS | Real pending-action test uses current v0.2 field names")
