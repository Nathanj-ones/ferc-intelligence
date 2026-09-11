"""Credential-free integration check: seed DB -> export -> validate -> browser data."""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

project = pathlib.Path(__file__).resolve().parents[1]
backend = pathlib.Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='ferc-integration-') as temporary:
    output = pathlib.Path(temporary)
    db = output / 'operating_assets.sqlite'
    source = sqlite3.connect((backend / 'staging/operating_assets.sqlite').as_uri() + '?mode=ro', uri=True)
    target = sqlite3.connect(db)
    source.backup(target)
    target.close()
    source.close()
    env = {**os.environ, 'FERC_API_KEY': '', 'FERC_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
           'FERC_STAGING_DB': str(db), 'FERC_OUTPUT_DIR': str(output),
           'FERC_SOURCE_CACHE': str(backend / 'source_cache'), 'FERC_LEDGER': str(output / 'ledger.json')}
    for command in ['export', 'validate']:
        subprocess.run([sys.executable, 'run.py', command, '--offline', '--as-of', '2026-09-07'],
                       cwd=backend, env=env, check=True)
    receipt = json.loads((output / 'publication_receipt.json').read_text())
    subprocess.run([shutil.which('node'), str(project / 'scripts/sync-ferc-backend.mjs')], cwd=project, check=True,
                   env={**env, 'FERC_BACKEND_ROOT': str(output), 'FERC_FRONTEND_DATA_ROOT': str(output / 'frontend'),
                        'FERC_EXPECTED_GENERATION': receipt['generation_id']})
    directory = json.loads((output / 'frontend/generations' / receipt['generation_id'] / 'directory.json').read_text())
    assert len(directory['assets']) == 120, 'Reviewed asset roster changed'
    print('Repository-only backend → validated export → frontend integration passed: 120 assets.')
