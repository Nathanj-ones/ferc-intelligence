# FERC Project Filter

Starter Python project for testing FERC's official Data API and building a project-side filing filter.

## Current objective

1. Discover official FERC Data API datasets.
2. Inspect dataset schemas and stable identifiers.
3. Determine what data can be retrieved programmatically.
4. Compare broad retrieval vs targeted retrieval where supported.
5. Apply the project-side ALERT / REVIEW / SUPPRESS logic to known ground-truth examples.

## Current known facts

- The official FERC Data API authentication works with an API key.
- The top-level `/v1/data-assets/` endpoint currently returns 10 data assets in the user's test environment.
- The Data Catalog does not currently expose an eLibrary filings dataset, so this project must not assume Data.FERC.gov is the direct source for eLibrary filing metadata.

## Setup in PowerShell

```powershell
$env:FERC_API_KEY="YOUR_KEY"
python -m pip install -e .
python scripts_smoke.py
python scripts_catalog.py
```

Never commit or paste the API key into source files.

## Baseline vs. ground truth

The `classifier.py` logic is intentionally conservative. A document such as a generic `Data Request` is classified as `REVIEW` until content-level rules are available, even though a manually reviewed example may later be labelled `SUPPRESS`. The `ground_truth.py` file records those manually validated final labels so we can measure false positives/negatives as the classifier matures.
