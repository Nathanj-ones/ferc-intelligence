export type LocalRefreshStatus = {
  enabled: true;
  credentialConfigured: boolean;
  status: 'idle' | 'running' | 'succeeded' | 'failed';
  phase: string;
  message: string;
  jobId?: string;
  generationId: string | null;
  lastSuccessfulAt: string | null;
};

export function isLocalFercSite() {
  return typeof window !== 'undefined' &&
    ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
}

export async function getLocalRefreshStatus(): Promise<LocalRefreshStatus | null> {
  if (!isLocalFercSite()) return null;
  const response = await fetch('/api/local-ferc/status', {
    cache: 'no-store', signal: AbortSignal.timeout(5000),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error('The local refresh service is unavailable.');
  // A normal dev server without the live plugin may fall through to HTML.
  if (!response.headers.get('content-type')?.includes('application/json')) return null;
  const value: unknown = await response.json();
  if (!value || typeof value !== 'object') throw new Error('Invalid local refresh status.');
  const result = value as Record<string, unknown>;
  if (result.enabled !== true || typeof result.status !== 'string' || !['idle', 'running', 'succeeded', 'failed'].includes(result.status) ||
    !(result.generationId === null || (typeof result.generationId === 'string' && /^[a-f0-9]{64}$/.test(result.generationId))) ||
    typeof result.credentialConfigured !== 'boolean' ||
    !(result.lastSuccessfulAt === null || (typeof result.lastSuccessfulAt === 'string' && Number.isFinite(Date.parse(result.lastSuccessfulAt)))) ||
    typeof result.phase !== 'string' || typeof result.message !== 'string') {
    throw new Error('The local refresh service returned an invalid status.');
  }
  return result as LocalRefreshStatus;
}
