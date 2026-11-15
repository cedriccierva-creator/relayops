'use client';

import { useState } from 'react';
import { Copy, Plus, Radio, Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { apiFetch } from '@/lib/api';
import { message, type Source, type Destination } from '@/lib/relay';
import { Badge } from './shared';

export function Configuration({ mode, sources, destinations, refresh }: { mode: 'Sources' | 'Destinations'; sources: Source[]; destinations: Destination[]; refresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [secret, setSecret] = useState('');
  const [url, setUrl] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [editing, setEditing] = useState<Destination | null>(null);
  const [enabled, setEnabled] = useState(true);
  const sourceMode = mode === 'Sources';
  function resetForm() {
    setEditing(null); setName(''); setSecret(''); setUrl(''); setEnabled(true);
  }
  function edit(item: Destination) {
    setEditing(item); setName(item.name); setUrl(item.url); setEnabled(item.enabled);
    setError(''); setNotice('');
    document.getElementById('connection-name')?.focus();
  }
  async function toggle(item: Source) {
    setBusy(true); setError(''); setNotice('');
    try {
      await apiFetch(`/api/${sourceMode ? 'sources' : 'destinations'}/${item.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !item.enabled }) });
      await refresh(); setNotice(`${item.name} ${item.enabled ? 'paused' : 'enabled'}.`);
    } catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <div className="configuration-grid">
    <section><div className="section-heading"><h2>Connected {mode.toLowerCase()}</h2><span className="count">{sourceMode ? sources.length : destinations.length}</span></div>
      {notice && <p className="notice" role="status">{notice}</p>}{error && <p className="error" role="alert">{error}</p>}
      {(sourceMode ? sources : destinations).map(item => <Card key={item.id} className="connection-card"><CardContent><div className="connection-title"><div className="connection-icon">{sourceMode ? <Radio size={19} /> : <Send size={19} />}</div><div><h3>{item.name}</h3>{!sourceMode && <small>From {sources.find(source => source.id === (item as Destination).source_id)?.name ?? 'source'}</small>}</div><Badge status={item.enabled ? 'enabled' : 'paused'} /></div>
        {sourceMode ? <><label>Webhook endpoint</label><div className="endpoint"><code>/api/hooks/{item.id}</code><Button variant="ghost" size="icon" aria-label={`Copy endpoint for ${item.name}`} onClick={async () => {
          try { await navigator.clipboard.writeText(`${window.location.origin}/api/hooks/${item.id}`); setNotice('Webhook URL copied.'); } catch { setError('Copy unavailable. Select and copy the endpoint manually.'); }
        }}><Copy size={15} /></Button></div><p className="hint">Authenticate with the X-Webhook-Secret header.</p></> : <p className="destination-url">{(item as Destination).url}</p>}
        <div className="connection-footer"><small>{sourceMode ? 'Secret stored as a hash' : 'POST · JSON · Automatic retries'}</small><div className="flex gap-2">{!sourceMode && <Button variant="outline" disabled={busy} onClick={() => edit(item as Destination)}>Edit</Button>}<Button variant="outline" disabled={busy || editing?.id === item.id} onClick={() => void toggle(item)}>{item.enabled ? 'Pause' : 'Enable'}</Button></div></div>
      </CardContent></Card>)}
      {!(sourceMode ? sources : destinations).length && <div className="empty"><Send size={28} /><h3>{sourceMode ? 'Start with a source' : 'Give your events a destination'}</h3><p>{sourceMode ? 'A source gives your sender a dedicated, protected webhook URL.' : 'Connect an HTTPS webhook endpoint, including n8n.'}</p></div>}
    </section>
    <Card className="config-form"><CardHeader><span className="eyebrow">CONNECT YOUR WORKFLOW</span><CardTitle><h2>{editing ? 'Edit destination' : `Add a ${sourceMode ? 'source' : 'destination'}`}</h2></CardTitle></CardHeader><CardContent><form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError(''); setNotice('');
      try {
        if (!name.trim()) throw new Error('Name must not be blank.');
        if (!sourceMode) validateDestinationUrl(url);
        const body = editing ? { name: name.trim(), url, enabled } : sourceMode ? { name: name.trim(), secret } : { name: name.trim(), url, source_id: sourceId || sources[0]?.id };
        await apiFetch(`/api/${sourceMode ? 'sources' : 'destinations'}${editing ? `/${editing.id}` : ''}`, { method: editing ? 'PATCH' : 'POST', body: JSON.stringify(body) });
        const savedEdit = Boolean(editing); resetForm(); await refresh(); setNotice(savedEdit ? 'Destination updated. Future attempts use the saved URL.' : `${sourceMode ? 'Source' : 'Destination'} connected.`);
      } catch (error) { setError(message(error)); } finally { setBusy(false); }
    }}>
      <label htmlFor="connection-name">Name</label><Input id="connection-name" required maxLength={100} placeholder={sourceMode ? 'e.g. Store orders' : 'e.g. n8n order workflow'} value={name} onChange={e => setName(e.target.value)} />
      {sourceMode ? <><label htmlFor="source-secret">Source secret</label><Input id="source-secret" type="password" required minLength={32} maxLength={256} autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} /><p className="hint">Use a random secret of at least 32 characters. Save it in your password manager; it cannot be retrieved later.</p></> : <><label htmlFor="destination-source">Source</label><select id="destination-source" required disabled={Boolean(editing)} value={editing?.source_id || sourceId || sources[0]?.id || ''} onChange={e => setSourceId(e.target.value)}>{!sources.length && <option value="">Create a source first</option>}{sources.map(source => <option key={source.id} value={source.id}>{source.name}{source.enabled ? '' : ' (paused)'}</option>)}</select><label htmlFor="destination-url">Destination URL</label><Input id="destination-url" type="url" required maxLength={2048} placeholder="https://n8n.example.com/webhook/orders" value={url} onChange={e => setUrl(e.target.value)} /><p className="hint">Use public HTTPS without credentials, query parameters, or fragments. Local HTTP requires an explicit development allowlist.</p></>}
      {editing && <><label htmlFor="destination-enabled">Delivery state</label><select id="destination-enabled" value={enabled ? 'enabled' : 'paused'} onChange={e => setEnabled(e.target.value === 'enabled')}><option value="enabled">Enabled</option><option value="paused">Paused</option></select><p className="hint">Queued retries and manual replays use the saved URL when the worker claims them. An in-flight attempt may finish with the previous URL. History is preserved.</p></>}
      <div className="flex gap-2"><Button type="submit" disabled={busy || (!sourceMode && !sources.length)}>{!editing && <Plus size={16} />}{busy ? 'Saving…' : editing ? 'Save changes' : `Add ${sourceMode ? 'source' : 'destination'}`}</Button>{editing && <Button type="button" variant="outline" disabled={busy} onClick={resetForm}>Cancel</Button>}</div>
    </form><div className="form-note">{sourceMode ? 'Include an Idempotency-Key or X-Event-ID header to safely deduplicate repeated requests.' : 'Pausing holds queued jobs and stops routing new events here. An in-flight delivery may still finish.'}</div></CardContent></Card>
  </div>;
}


function validateDestinationUrl(value: string) {
  let parsed: URL;
  try { parsed = new URL(value); } catch { throw new Error('Enter a valid destination URL.'); }
  if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password || value.includes('?') || value.includes('#')) {
    throw new Error('Use an HTTP(S) URL without credentials, query parameters, or fragments.');
  }
  // The backend remains authoritative for DNS, HTTPS and development exceptions.
}
