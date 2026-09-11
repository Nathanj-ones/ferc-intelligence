import assert from 'node:assert/strict';
import { test } from 'node:test';
import path from 'node:path';
import { backendPaths } from './backend-config.mjs';

test('backend defaults resolve inside any checkout, not the original machine', () => {
  const root = path.resolve('arbitrary-clone');
  const config = backendPaths(root);
  assert.equal(config.backend, path.join(root, 'backend'));
  assert.equal(config.credentialFile, path.join(root, '.env'));
  assert.equal(config.python, process.env.FERC_LOCAL_PYTHON || 'python3');
});
