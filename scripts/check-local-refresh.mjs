import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { LocalRefresh, trustedLocalRequest } from './local-ferc-refresh.mjs';

// Tests inject a fake credential into this test process only; no network runner executes.
process.env.FERC_API_KEY = 'local-refresh-test-not-a-credential';

test('refresh requires loopback, exact Origin, JSON and a custom CSRF header', () => {
  const good = { headers: { host: 'localhost:5173', origin: 'http://localhost:5173', 'content-type': 'application/json', 'x-ferc-local-refresh': '1' }, socket: { remoteAddress: '127.0.0.1' } };
  assert.equal(trustedLocalRequest(good, true), true);
  for (const headers of [
    { origin: 'https://evil.example' }, { origin: undefined },
    { host: 'localhost.evil.example:5173' }, { 'x-ferc-local-refresh': undefined },
    { 'content-type': 'text/plain' }, { 'sec-fetch-site': 'cross-site' },
  ]) assert.equal(trustedLocalRequest({ ...good, headers: { ...good.headers, ...headers } }, true), false);
  assert.equal(trustedLocalRequest({ ...good, socket: { remoteAddress: '192.168.1.8' } }, true), false);
});

async function fixture(t, run) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ferc-refresh-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const manager = await new LocalRefresh(root, { run }).init();
  const work = path.join(manager.root, 'working');
  await fs.mkdir(path.join(work, 'output'), { recursive: true });
  await fs.writeFile(path.join(work, 'initialized.json'), '{}');
  return manager;
}

test('a complete validated refresh activates only after frontend verification', async (t) => {
  const phases = [];
  const generation = 'a'.repeat(64);
  const manager = await fixture(t, async (_command, args, env, label) => {
    phases.push(label);
    assert.equal(manager.active, null, 'No partial activation');
    if (label === 'Pulling live source data') {
      assert.ok(args.includes('refresh'));
      assert.ok(!args.includes('--offline'));
      assert.ok(args.includes('--budget'));
    }
    if (label === 'Building source snapshot') await fs.writeFile(path.join(env.FERC_OUTPUT_DIR, 'publication_receipt.json'), JSON.stringify({ generation_id: generation }));
    if (label === 'Verifying frontend data and evidence') {
      assert.equal(env.FERC_EXPECTED_GENERATION, generation);
      await fs.mkdir(env.FERC_FRONTEND_DATA_ROOT, { recursive: true });
      await fs.writeFile(path.join(env.FERC_FRONTEND_DATA_ROOT, 'current.json'), JSON.stringify({ generationId: generation }));
    }
  });
  await manager.start();
  assert.equal(await manager.start(), false, 'Concurrent refresh is rejected');
  await manager.job;
  assert.equal(manager.state.status, 'succeeded');
  assert.equal(manager.active.generationId, generation);
  assert.deepEqual(phases, ['Pulling live source data', 'Recomputing coverage', 'Recomputing field statuses', 'Building source snapshot', 'Validating source snapshot', 'Verifying frontend data and evidence']);
  assert.equal((await new LocalRefresh(manager.project).init()).active.generationId, generation, 'Pointer survives restart');
});

test('source and validation failures retain the last good pointer and allow retry', async (t) => {
  for (const phase of ['Pulling live source data', 'Validating source snapshot']) {
    const manager = await fixture(t, async (_command, _args, _env, label) => {
      if (label === phase) throw new Error('Controlled failure');
    });
    manager.active = { generationId: 'b'.repeat(64), refreshedAt: '2026-09-01T00:00:00Z' };
    await manager.start();
    await manager.job;
    assert.equal(manager.state.status, 'failed');
    assert.equal(manager.active.generationId, 'b'.repeat(64));
    assert.equal(await manager.start(), true);
    await manager.job;
  }
});

test('an interrupted refresh is recovered as failed on restart', async (t) => {
  const manager = await fixture(t, async () => {});
  await manager.update({ status: 'running', phase: 'Pulling live source data' });
  const restarted = await new LocalRefresh(manager.project).init();
  assert.equal(restarted.state.status, 'failed');
  assert.match(restarted.state.message, /retry/);
});
