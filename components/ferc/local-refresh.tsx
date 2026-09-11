'use client';

import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { getLocalRefreshStatus, isLocalFercSite, type LocalRefreshStatus } from '@/lib/ferc/local-refresh';

export function LocalDataRefresh({ generationId }: { generationId?: string }) {
  const [status, setStatus] = useState<LocalRefreshStatus | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const startedHere = useRef(false);

  useEffect(() => {
    if (!isLocalFercSite()) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await getLocalRefreshStatus();
        if (stopped) return;
        setStatus(next);
        setError('');
        if (!next) return;
        if (next?.status === 'succeeded' && startedHere.current) {
          startedHere.current = false;
          window.location.reload();
          return;
        }
        if (next?.status === 'failed') startedHere.current = false;
      } catch {
        if (!stopped) setError('Cannot reach the local refresh service. Check that the local server is running.');
      }
      if (!stopped) timer = setTimeout(poll, 2500);
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);

  async function refresh() {
    setSubmitting(true);
    setError('');
    try {
      const response = await fetch('/api/local-ferc/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ferc-Local-Refresh': '1' },
        body: '{}', signal: AbortSignal.timeout(10000),
      });
      if (response.status !== 202 && response.status !== 409) throw new Error('Refresh request failed');
      const next = await getLocalRefreshStatus();
      setStatus(next);
      startedHere.current = true;
    } catch {
      setError('Could not confirm the refresh request. Status will reconnect automatically; the current data is unchanged.');
    } finally { setSubmitting(false); }
  }

  if (!status) return error ? <output className="local-refresh-panel">{error}</output> : null;
  const busy = submitting || status.status === 'running';
  const newSnapshot = status.generationId && generationId && status.generationId !== generationId;
  return (
    <section className="local-refresh-panel" aria-label="Local live data refresh">
      <output className="local-refresh-copy" aria-live="polite" aria-atomic="true">
        <strong>{busy ? status.phase : 'Local source refresh'}</strong>
        <span className="local-refresh-message">{error || status.message}</span>
        {!busy && status.lastSuccessfulAt && <small>Last successful pull: {new Date(status.lastSuccessfulAt).toLocaleString()}</small>}
        {!status.credentialConfigured && <small>FERC_API_KEY is required on the local server.</small>}
      </output>
      <div className="local-refresh-actions">
        {newSnapshot && <Button variant="outline" onClick={() => window.location.reload()}>Load refreshed snapshot</Button>}
        <Button variant="outline" disabled={busy || !status.credentialConfigured} onClick={() => void refresh()}>
          <RefreshCw aria-hidden="true" className={busy ? 'animate-spin motion-reduce:animate-none' : ''} />
          {busy ? 'Refreshing…' : 'Refresh live data'}
        </Button>
      </div>
    </section>
  );
}
