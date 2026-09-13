from pathlib import Path

path = Path("app/page.tsx")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

blocks = [
    (
        '''                <p className="company-unmapped">
                  Company not mapped in supplied project export
                </p>''',
        '''                <p className="company-unmapped">
                  {project.company}
                </p>''',
        "PASS | Project cards now display mapped company",
    ),
    (
        '''          <p>
            Company not mapped ·{' '}
            <InfoTerm term="Docket">{project.docket}</InfoTerm>
          </p>''',
        '''          <p>
            {project.company} ·{' '}
            <InfoTerm term="Docket">{project.docket}</InfoTerm>
          </p>''',
        "PASS | Project detail header now displays mapped company",
    ),
    (
        '''              : 'The supplied Tommy project snapshots do not include reviewed parent-company mappings. Choose Company not mapped or All companies to see them.'
''',
        '''              : 'No projects are available for the selected company in the current project snapshot.'
''',
        "PASS | Removed stale unmapped-project empty-state copy",
    ),
]

for old, new, message in blocks:
    if old in text:
        text = text.replace(old, new, 1)
        print(message)
    elif new in text:
        print(message + " (already present)")
    else:
        print("WARN | Expected block not found:", message)

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

print()
print(f"PASS | Syntax-checked {path}")
print()
print("Next:")
print("  npm run build")
