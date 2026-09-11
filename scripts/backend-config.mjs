import fs from 'node:fs/promises';
import path from 'node:path';

export function backendPaths(project) {
  const root = path.resolve(project);
  return {
    project: root,
    backend: path.resolve(
      process.env.FERC_LOCAL_BACKEND_ROOT || path.join(root, 'backend'),
    ),
    credentialFile: path.resolve(
      process.env.FERC_LOCAL_ENV_FILE || path.join(root, '.env'),
    ),
    python: process.env.FERC_LOCAL_PYTHON || 'python3',
  };
}

export async function backendCredential(project) {
  if (process.env.FERC_API_KEY?.trim()) return process.env.FERC_API_KEY.trim();
  try {
    const content = await fs.readFile(
      backendPaths(project).credentialFile,
      'utf8',
    );
    const match = content.match(
      /^\s*(?:export\s+)?FERC_API_KEY\s*=\s*(.*?)\s*$/m,
    );
    if (!match) return '';
    const value = match[1];
    return /^(['"])(.*)\1$/.test(value)
      ? value.slice(1, -1)
      : value.replace(/\s+#.*$/, '').trim();
  } catch {
    return '';
  }
}
