import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { backendPaths, backendCredential } from './backend-config.mjs';

const project = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
);
const config = backendPaths(project);
const [action, ...args] = process.argv.slice(2);
const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };
function run(command, argv, cwd = config.backend, environment = env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, argv, {
      cwd,
      env: environment,
      stdio: 'inherit',
      shell: false,
    });
    child.once('error', reject);
    child.once('close', (code) =>
      code === 0
        ? resolve()
        : reject(new Error(`Command did not complete (exit ${code}).`)),
    );
  });
}
try {
  await run(config.python, [
    '-c',
    'import sys,sqlite3; assert (3,9)<=sys.version_info[:2]<(3,15), "Use Python 3.9–3.14"; assert sqlite3.sqlite_version_info >= (3,24,0)',
  ]);
  if (action === 'setup') {
    await run(
      config.python,
      [path.join(project, 'scripts/setup-backend.py'), ...args],
      project,
    );
  } else if (action === 'test') {
    try {
      await fs.access(
        path.join(config.backend, 'staging/bootstrap-installed.json'),
      );
    } catch {
      throw new Error(
        'Run npm run setup:backend before the fixture-backed backend test suite.',
      );
    }
    const temporary = await fs.mkdtemp(
      path.join(project, '.ferc-local/backend-tests-'),
    );
    try {
      const db = path.join(temporary, 'operating_assets.sqlite');
      await run(config.python, [
        '-c',
        'import sqlite3,sys,pathlib; src=sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri()+"?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()',
        path.join(config.backend, 'staging/operating_assets.sqlite'),
        db,
      ]);
      await run(
        config.python,
        [
          '-m',
          'unittest',
          'discover',
          '-s',
          'tests',
          '-t',
          '.',
          '-p',
          'test*.py',
        ],
        config.backend,
        {
          ...env,
          FERC_OFFLINE: '1',
          FERC_STAGING_DB: db,
          FERC_OUTPUT_DIR: temporary,
          FERC_SOURCE_CACHE: path.join(config.backend, 'source_cache'),
          FERC_LEDGER: path.join(temporary, 'ledger.json'),
        },
      );
    } finally {
      await fs.rm(temporary, { recursive: true, force: true });
    }
  } else if (action === 'check') {
    await run(
      config.python,
      ['run.py', 'plan', '--check', ...args],
      config.backend,
      {
        ...env,
        FERC_SOURCE_CACHE: path.join(config.backend, 'source_cache'),
        FERC_OFFLINE: '1',
      },
    );
  } else if (action === 'verify') {
    await run(
      config.python,
      [path.join(project, 'scripts/verify-backend.py'), config.backend],
      project,
    );
  } else if (action === 'run') {
    // Explicit CLI commands use disposable working state, never the reviewed seed.
    const working = path.join(project, '.ferc-local/cli');
    await fs.mkdir(working, { recursive: true, mode: 0o700 });
    const db = path.join(working, 'operating_assets.sqlite');
    try {
      await fs.access(db);
    } catch {
      await run(config.python, [
        '-c',
        'import sqlite3,sys,pathlib; src=sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri()+"?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()',
        path.join(config.backend, 'staging/operating_assets.sqlite'),
        db,
      ]);
    }
    const cache = path.join(working, 'source_cache');
    try {
      await fs.access(path.join(cache, 'index.json'));
    } catch {
      await fs.cp(path.join(config.backend, 'source_cache'), cache, {
        recursive: true,
      });
    }
    await run(
      config.python,
      ['run.py', ...(args.length ? args : ['status'])],
      config.backend,
      {
        ...env,
        FERC_API_KEY: await backendCredential(project),
        FERC_STAGING_DB: db,
        FERC_SOURCE_CACHE: cache,
        FERC_OUTPUT_DIR: path.join(working, 'output'),
        FERC_LEDGER: path.join(working, 'task_ledger.json'),
      },
    );
  } else throw new Error('Unknown backend task');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
