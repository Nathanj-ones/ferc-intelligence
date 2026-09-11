# Local site and source refresh

On a fresh clone, run `npm ci` and `npm run setup:backend`, copy `.env.example`
to `.env`, and add your FERC key. Run `npm run dev:live`, then open
[localhost:5173](http://localhost:5173/?view=assets).
Keep the terminal running while using the site or refreshing data.

The **Refresh live data** button pulls from the backend's configured live source
adapters. It is not merely a browser reload and it is not a continuous feed.
The original reviewed snapshot is shown until a refreshed snapshot passes all
checks. After a successful pull, the initiating tab reloads the same page;
other tabs offer **Load refreshed snapshot**.

## What runs

1. Setup downloads a checksum-pinned reviewed seed and source cache from the
   private release attached to this same GitHub repository. On first refresh,
   create a coherent SQLite backup of that repository-local backend seed and
   copy its source cache into the ignored, private `.ferc-local/working` directory.
   Allow several GB of disk space. The frozen backend and published files are
   not updated.
2. Run the backend's online `refresh` command for 2024 through the current year,
   limited to 2,000 requests and a 20-minute overall job deadline.
3. Recompute coverage and per-field statuses, export a candidate source generation,
   and run the backend's publication and data validation.
4. Verify the frontend projection, including source dependency hashes and evidence,
   then atomically select that local generation. Existing browser tabs keep a
   coherent generation until reloaded.

Any incomplete source run (including a partial result, timeout, or validation
failure) leaves the last good displayed snapshot selected. A failed attempt can
be retried. Refresh does not automatically approve ambiguous review records,
invent missing metrics, or implement unsupported source adapters. Some backend
inputs are reviewed historical captures, so not every field is live-retrievable.

The button shows progress, failures, and the last successful refresh time.
Only one refresh can write at a time. Closing the server interrupts a running
pull; the next start reports it as interrupted and offers a retry.

## Configuration and security

- The server reads `FERC_API_KEY` from its environment or the repository-root
  `.env` file. The key is not returned to the browser.
- Optional server-only overrides: `FERC_LOCAL_ENV_FILE`, `FERC_LOCAL_BACKEND_ROOT`,
  and `FERC_LOCAL_PYTHON`. Configure the backend location before first use; do not
  reuse an existing working database with an unrelated backend.
- Source code defaults to this repository's `backend/`. Python defaults to
  `python3` on PATH; use Python 3.9–3.14. Full image-only capacity extraction
  requires the existing macOS Vision/Swift and Poppler runtime; see the README.
- Refresh requires a loopback connection, an exact same-origin request, and a
  custom request header. Working files are denied by the development file server.
- This service exists only in `dev:live`. The deployed private site is unchanged
  and continues to use its pinned release. Publishing a new hosted snapshot is a
  separate, reviewed operation.

## Verification

`npm test` covers request authorization, single-writer behavior, ordered validation
before activation, retained data after source/validation failures, and recovery
after interruption, alongside the existing FERC data regression checks.
Use `npm run lint`, `npx tsc --noEmit`, and `npm run build` for the frontend checks.
