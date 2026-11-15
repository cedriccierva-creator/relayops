'use client';

import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { apiFetch } from '@/lib/api';
import { dateTime, deliveryState, failureReason, message, type Attempt, type Destination, type EventDetail as Detail } from '@/lib/relay';
import { Badge } from './shared';

export function EventDetail({ id, destinations, onBack, onChange }: { id: string; destinations: Destination[]; onBack: () => void; onChange: () => Promise<void> }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [history, setHistory] = useState<Record<string, Attempt[]>>({});
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(async (signal?: AbortSignal) => {
    const event = await apiFetch<Detail>(`/api/events/${id}`, { signal });
    const histories = await Promise.all(event.jobs.map(async job => [job.id, await apiFetch<Attempt[]>(`/api/deliveries/${job.id}/attempts`, { signal })] as const));
    if (!signal?.aborted) { setDetail(event); setHistory(Object.fromEntries(histories)); setError(''); }
  }, [id]);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => { void load(controller.signal).catch(error => { if (!controller.signal.aborted) setError(message(error)); }); };
    refresh(); const timer = setInterval(refresh, 10_000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [load]);
  return <section className="event-detail"><Button variant="ghost" onClick={onBack} className="back-button"><ArrowLeft size={16} />Back to events</Button>
    {error && <p className="error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    {!detail ? <p role="status">{error ? 'Event could not be loaded.' : 'Loading event…'}</p> : <>
      <div className="section-heading"><div><span className="eyebrow">EVENT INSPECTOR</span><h1>{detail.source_name}</h1><code className="full-id">{detail.id}</code></div><Badge status={detail.status} /></div>
      <div className="event-meta"><span>Received <strong>{dateTime(detail.received_at)}</strong></span><span>Payload <strong>{detail.size_bytes.toLocaleString()} bytes</strong></span><span>Destinations <strong>{detail.delivery_count}</strong></span></div>
      <div className="detail-grid"><section><div className="section-heading"><h2>JSON payload</h2><span className="eyebrow">APPLICATION/JSON</span></div><pre className="payload">{JSON.stringify(detail.payload, null, 2)}</pre><p className="hint">Known sensitive fields are redacted in this view. Original payload bytes are forwarded.</p><details><summary>Request metadata</summary><pre>{JSON.stringify(detail.request_metadata, null, 2)}</pre></details></section>
      <section><div className="section-heading"><h2>Delivery history</h2><span className="count">{detail.jobs.length}</span></div>
        {!detail.jobs.length && <div className="empty"><h3>No destinations at receipt</h3><p>Add an enabled destination before sending your next event.</p></div>}
        {detail.jobs.map(job => {
          const destination = destinations.find(item => item.id === job.destination_id);
          const latest = history[job.id]?.[0];
          return <Card key={job.id} className="delivery-card"><CardContent><div className="section-heading"><h3>{destination?.name || 'Destination'}</h3><Badge status={job.status} /></div><p className="hint">{job.attempts_total} total attempts · {job.cycle_attempts}/{job.max_attempts} in this cycle</p>
            <p><strong>{deliveryState(job, latest, destination)}</strong></p>
            {latest && <p className="hint">Last attempt: {dateTime(latest.started_at)}{latest.status_code !== null ? ` · HTTP ${latest.status_code}` : ''}</p>}
            {latest?.error && <p className="hint">Last failure: {failureReason(latest)}</p>}
            {job.status === 'queued' && <p className="hint">{destination?.enabled === false ? 'Paused until the destination is enabled.' : `Next eligible attempt: ${dateTime(job.next_attempt_at)}`}</p>}
            {job.status === 'failed' && <p className="hint">Automatic delivery stopped. Review the destination, then replay this event when ready.</p>}
            {job.status === 'failed' && <Button variant="outline" disabled={busy !== null || destination?.enabled === false} onClick={async () => {
              setBusy(job.id); setError(''); setNotice('');
              try { await apiFetch(`/api/deliveries/${job.id}/retry`, { method: 'POST' }); await load(); await onChange(); setNotice('Delivery queued for another retry cycle.'); }
              catch (error) { setError(message(error)); } finally { setBusy(null); }
            }}><RotateCcw size={14} />{busy === job.id ? 'Queueing…' : 'Retry delivery'}</Button>}
            {job.status === 'failed' && destination?.enabled === false && <p className="hint">Enable the destination before retrying.</p>}
            <div className="attempts">{(history[job.id] || []).map(attempt => <div className="attempt" key={attempt.id}><div className="attempt-top"><strong>Attempt {attempt.number}</strong><span className={attempt.error ? 'error-text' : ''}>{attempt.status_code !== null ? `HTTP ${attempt.status_code}` : attempt.finished_at ? 'No response' : 'In progress'}</span></div><small>{dateTime(attempt.started_at)} · {attempt.duration_ms === null ? 'Duration unavailable' : `${attempt.duration_ms} ms`}</small>{attempt.error && <p className="error-text">{failureReason(attempt)}</p>}{attempt.response_metadata.content_type && <small>{attempt.response_metadata.content_type}</small>}</div>)}{!history[job.id]?.length && <p className="hint">Waiting for the worker to attempt delivery.</p>}</div>
          </CardContent></Card>;
        })}<p className="hint">Response bodies and sensitive headers are not retained.</p>
      </section></div>
    </>}
  </section>;
}
