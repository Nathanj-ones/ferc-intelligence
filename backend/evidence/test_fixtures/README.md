# Acceptance evidence fixtures

These fixtures replace the former implicit sibling-tree "baseline" and ad-hoc
personal image/exception paths. Every consumer reads them through
`acceptance.harness.fixture_bytes` / `fixture_json`, which requires the exact
release-relative file, rejects symlinks, and verifies byte count and SHA-256
against `MANIFEST.json`. Missing, aliased, unlisted or changed evidence is a
test failure—not a skip and not a zero-row pass.

The fixtures have two deliberately separate jobs:

- `historical_*`, `pre_repair_schema.json`, and
  `form549d_historical_semantic_exceptions.json` preserve exact historical
  negative populations from the independent audit. They prove that a detector
  catches a known defect; they are not expected output for repaired code.
- `runtime_seed_observation.json` and `reviewed_image_sources.json` supply
  schema-complete or independently reviewed inputs for positive and negative
  controls. Tests rewrite the seed identity/value to `W6ACC_SYNTHETIC` before
  insertion. No reference answer is regenerated with the implementation under
  test.

Primary provenance is recorded inside each JSON object. The historical database
used for the read-only extracts was 707,710,976 bytes with SHA-256
`2fcc750b694fbad6deec6b5ab08f25162f81b4fa48a5e2b4a4cb37d423a1dd3b`.
The source database is intentionally not shipped as a test dependency.

`reviewed_image_sources.json` is a compact record of the independent visual
review, including exact PDF and rendered-page hashes. It exercises the shipped
reviewed-page transcription/canonicalisation path. It does not rerun Poppler or
constitute a new visual review, because duplicating the source PDFs/rendered
PNGs is outside this compact test-evidence set. That boundary is explicit so a
green test cannot be reported as fresh OCR or image-parser validation.
