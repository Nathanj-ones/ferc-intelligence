// Node-only development middleware. Never installed in the hosted Worker.
import fs from 'node:fs/promises';
import { constants, createReadStream } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';

const DEFAULT_BACKEND = '/Users/nathanjones/Desktop/ferc_reaudit_handoff/operating_assets_backend_ready_20260910/operating_assets_all_regimes';
const DEFAULT_CREDENTIALS = '/Users/nathanjones/Desktop/ferc/.env';
const GENERATION = /^[a-f0-9]{64}$/;
const loopback = (host) => ['localhost', '127.0.0.1', '[::1]'].includes(host);
const readJson = async (file) => JSON.parse(await fs.readFile(file, 'utf8'));
async function optionalJson(file) {
  try { return await readJson(file); } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}
async function atomicJson(file, data) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(data)}\n`, { mode: 0o600 });
  await fs.rename(temporary, file);
}

export function trustedLocalRequest(req, mutation = false) {
  try {
    const url = new URL(`http://${req.headers.host}`);
    const remote = req.socket.remoteAddress;
    if (!loopback(url.hostname) || !['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(remote)) return false;
    if (req.headers['sec-fetch-site'] === 'cross-site') return false;
    if (!mutation) return true;
    return req.headers.origin === url.origin &&
      req.headers['x-ferc-local-refresh'] === '1' &&
      req.headers['content-type'] === 'application/json';
  } catch { return false; }
}

async function credential() {
  if (process.env.FERC_API_KEY?.trim()) return process.env.FERC_API_KEY.trim();
  try {
    const content = await fs.readFile(process.env.FERC_LOCAL_ENV_FILE || DEFAULT_CREDENTIALS, 'utf8');
    const match = content.match(/^\s*(?:export\s+)?FERC_API_KEY\s*=\s*(.+?)\s*$/m);
    return match?.[1]?.replace(/^(['"])(.*)\1$/, '$2').trim() || '';
  } catch { return ''; }
}

export class LocalRefresh {
  constructor(root, options = {}) {
    this.root = path.join(root, '.ferc-local');
    this.project = root;
    this.backend = process.env.FERC_LOCAL_BACKEND_ROOT || DEFAULT_BACKEND;
    this.child = null;
    this.run = options.run || ((...args) => this.execute(...args));
    this.state = { status: 'idle', phase: 'Ready', message: 'Pull current source data, then validate. This can take several minutes.' };
    this.active = null;
  }
  async init() {
    await fs.mkdir(this.root, { recursive: true, mode: 0o700 });
    this.active = await optionalJson(path.join(this.root, 'active.json'));
    if (this.active && !GENERATION.test(this.active.generationId)) throw new Error('Invalid local snapshot pointer');
    const previous = await optionalJson(path.join(this.root, 'status.json'));
    if (previous) this.state = previous.status === 'running'
      ? { ...previous, status: 'failed', phase: 'Interrupted', message: 'The local server stopped during refresh. The last good snapshot is unchanged; you can retry.' }
      : previous;
    return this;
  }
  async status() {
    return { enabled: true, credentialConfigured: Boolean(await credential()), ...this.state,
      generationId: this.active?.generationId || null,
      lastSuccessfulAt: this.active?.refreshedAt || null };
  }
  async update(data) {
    this.state = { ...this.state, ...data };
    await atomicJson(path.join(this.root, 'status.json'), this.state);
  }
  async start() {
    if (this.state.status === 'running') return false;
    // Claim the job before any await so two tabs cannot start two writers.
    this.state = { status: 'running', jobId: randomUUID(), startedAt: new Date().toISOString(), phase: 'Preparing', message: 'Preparing an isolated local working copy…' };
    this.job = this.perform().catch(async () => {
      await this.update({ status: 'failed', completedAt: new Date().toISOString(),
        message: `Refresh stopped during ${this.state.phase.toLowerCase()}. The last good snapshot is unchanged. See the local server terminal for the failing stage.` });
    });
    return true;
  }
  async execute(command, args, env, label) {
    const remaining = this.deadline - Date.now();
    if (remaining <= 0) throw new Error('Refresh time limit reached');
    await this.update({ phase: label, message: `${label}… The current snapshot remains available.` });
    await new Promise((resolve, reject) => {
      const child = spawn(command, args, { cwd: this.backend, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
      this.child = child;
      // Drain output without exposing source URLs, credentials, or document text.
      child.stdout.resume();
      child.stderr.resume();
      const timer = setTimeout(() => child.kill('SIGKILL'), remaining);
      child.once('error', (error) => { clearTimeout(timer); reject(error); });
      child.once('close', (code) => {
        clearTimeout(timer);
        this.child = null;
        if (code === 0) resolve();
        else {
          console.error(`[FERC local refresh] ${label} did not complete (exit ${code ?? 'timeout/interrupted'}). No new snapshot activated.`);
          reject(new Error(`${label} failed`));
        }
      });
    });
  }
  async perform() {
    this.deadline = Date.now() + 20 * 60 * 1000;
    await this.update({});
    const key = await credential();
    if (!key) {
      await this.update({ status: 'failed', phase: 'Configuration', message: 'FERC_API_KEY is missing. Configure it in the server environment or the existing backend .env file, then retry.' });
      return;
    }
    const python = process.env.FERC_LOCAL_PYTHON || '/usr/bin/python3';
    const work = path.join(this.root, 'working');
    const db = path.join(work, 'operating_assets.sqlite');
    const cache = path.join(work, 'source_cache');
    const output = path.join(work, 'output');
    const env = { ...process.env, FERC_API_KEY: key, PYTHONDONTWRITEBYTECODE: '1',
      FERC_STAGING_DB: db, FERC_OUTPUT_DIR: output, FERC_SOURCE_CACHE: cache,
      FERC_LEDGER: path.join(work, 'task_ledger.json') };
    await fs.mkdir(work, { recursive: true, mode: 0o700 });
    if (!await optionalJson(path.join(work, 'initialized.json'))) {
      // SQLite's backup API captures WAL contents coherently; plain file copying does not.
      const temporaryDb = path.join(work, `seed-${randomUUID()}.sqlite`);
      await this.run(python, ['-c', 'import sqlite3,sys,pathlib; src=sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri()+"?mode=ro",uri=True); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()', path.join(this.backend, 'staging/operating_assets.sqlite'), temporaryDb], env, 'Copying reviewed database');
      await fs.chmod(temporaryDb, 0o600);
      await fs.rename(temporaryDb, db);
      await this.update({ phase: 'Copying source cache', message: 'First refresh: preparing a private cache (about 2 GB of local working data).' });
      await fs.cp(path.join(this.backend, 'source_cache'), cache, { recursive: true, mode: constants.COPYFILE_FICLONE, force: true });
      // The handoff is frozen; cloned directories must accept new cache objects.
      const writableDirectories = async (directory) => {
        await fs.chmod(directory, 0o700);
        for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
          if (entry.isSymbolicLink()) throw new Error('Source-cache symlinks are not supported');
          if (entry.isDirectory()) await writableDirectories(path.join(directory, entry.name));
        }
      };
      await writableDirectories(cache);
      await atomicJson(path.join(work, 'initialized.json'), { backend: this.backend });
    }
    const today = new Date().toISOString().slice(0, 10);
    const common = ['--as-of', today, '--year-from', '2024', '--year-to', today.slice(0, 4)];
    const cli = (command, args, label) => this.run(python, [path.join(this.backend, 'run.py'), command, ...common, ...args], env, label);
    await cli('refresh', ['--budget', '2000'], 'Pulling live source data');
    await cli('coverage', ['--offline'], 'Recomputing coverage');
    await this.run(python, [path.join(this.backend, 'build_field_status.py'), '--db', db, '--out', path.join(output, 'exports')], env, 'Recomputing field statuses');
    await cli('export', ['--offline'], 'Building source snapshot');
    await cli('validate', ['--offline'], 'Validating source snapshot');
    const receipt = await readJson(path.join(output, 'publication_receipt.json'));
    if (!GENERATION.test(receipt.generation_id)) throw new Error('Invalid publication generation');
    const presentation = path.join(this.root, 'data');
    await this.run(process.execPath, [path.join(this.project, 'scripts/sync-ferc-backend.mjs')], {
      ...env, FERC_BACKEND_ROOT: output, FERC_FRONTEND_DATA_ROOT: presentation,
      FERC_EXPECTED_GENERATION: receipt.generation_id,
    }, 'Verifying frontend data and evidence');
    const pointer = await readJson(path.join(presentation, 'current.json'));
    if (pointer.generationId !== receipt.generation_id) throw new Error('Publication pointer mismatch');
    const active = { generationId: pointer.generationId, refreshedAt: new Date().toISOString() };
    await atomicJson(path.join(this.root, 'active.json'), active);
    this.active = active;
    await this.update({ status: 'succeeded', phase: 'Complete', completedAt: active.refreshedAt,
      message: 'Source refresh and validation passed. The new snapshot is ready.' });
  }
}

export function localFercRefresh() {
  return {
    name: 'ferc-local-refresh', apply: 'serve',
    async configureServer(server) {
      const manager = await new LocalRefresh(server.config.root).init();
      const json = (res, code, data) => {
        res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
        res.end(JSON.stringify(data));
      };
      server.httpServer?.once('close', () => manager.child?.kill('SIGKILL'));
      server.middlewares.use(async (req, res, next) => {
        const pathname = (req.url || '').split('?')[0];
        const api = pathname.startsWith('/api/local-ferc/');
        const match = pathname.match(/^\/data\/ferc\/generations\/([a-f0-9]{64})\/(.+)$/);
        if (!api && !match) return next();
        if (!trustedLocalRequest(req, req.method === 'POST')) return json(res, 403, { error: 'Only same-origin localhost requests are allowed.' });
        try {
          if (pathname === '/api/local-ferc/status' && req.method === 'GET') return json(res, 200, await manager.status());
          if (pathname === '/api/local-ferc/refresh' && req.method === 'POST') {
            let body = '';
            for await (const chunk of req) {
              body += chunk.toString();
              if (body.length > 32) return json(res, 413, { error: 'Request too large.' });
            }
            if (body.trim() !== '{}') return json(res, 400, { error: 'Refresh takes no parameters.' });
            if (!await manager.start()) return json(res, 409, await manager.status());
            return json(res, 202, await manager.status());
          }
          if (api) return json(res, 404, { error: 'Unknown local refresh route.' });
          if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, { error: 'Method not allowed.' });
          const [, generation, relative] = match;
          const base = path.join(manager.root, 'data', 'generations', generation);
          const manifest = await optionalJson(path.join(base, 'manifest.json'));
          if (!manifest) return next(); // The original pinned snapshot is served normally.
          if (relative !== 'manifest.json' && !Object.hasOwn(manifest.files, relative)) return json(res, 404, { error: 'Not in snapshot manifest.' });
          const file = path.resolve(base, relative);
          if (!(await fs.realpath(file)).startsWith(`${await fs.realpath(base)}${path.sep}`)) return json(res, 403, { error: 'Invalid snapshot path.' });
          res.writeHead(200, { 'Content-Type': relative.endsWith('.gz') ? 'application/gzip' : 'application/json', 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff' });
          if (req.method === 'HEAD') return res.end();
          createReadStream(file).on('error', () => res.destroy()).pipe(res);
        } catch {
          if (!res.headersSent) json(res, 500, { error: 'The local refresh service could not complete this request.' });
          else res.destroy();
        }
      });
    },
  };
}
