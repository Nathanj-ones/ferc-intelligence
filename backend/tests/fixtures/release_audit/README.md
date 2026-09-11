# Historical audit fixtures

These unchanged records are regression inputs, not commands to execute or claims
about the current repository's test results. Historical absolute paths inside
them describe the old audit environment and are never opened by these tests.

- `EXCEPTION_DISPOSITIONS_DRAFT.json`: the original implementation-owned exception
  draft, retained from the integrated repair candidate to exercise finalization.
- `FINAL_TEST_RUN_SPEC_EXECUTED.json`: the recorded r5 Build-A test specification,
  retained to test package-qualified discovery and issue/exception test naming.

The backend-ready handoff omitted its mutable draft files while retaining two
tests that tried to open them. The monorepo points those tests at these explicit,
versioned fixtures instead. No test expectation or production metric is changed.
