'use client';

import Image from 'next/image';
import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowRight, CheckCheck, ChevronLeft, ChevronRight, Inbox, LayoutDashboard, LogOut, Radio, RefreshCw, Send, ShieldCheck, Workflow } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { ApiError, apiFetch } from '@/lib/api';
import { message, type Destination, type RelayEvent, type Source, type Stats, type User } from '@/lib/relay';
import { Auth } from './auth';
import { Configuration } from './configuration';
import { EventDetail } from './event-detail';
import { EventTable } from './shared';

type View = 'Overview' | 'Events' | 'Sources' | 'Destinations';
const navigation = [{ name: 'Overview', icon: LayoutDashboard }, { name: 'Events', icon: Inbox }, { name: 'Sources', icon: Radio }, { name: 'Destinations', icon: Send }] as const;

export function RelayDashboard() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    apiFetch<User>('/api/auth/me', { signal: controller.signal }).then(setUser).catch(error => {
      if (!controller.signal.aborted && !(error instanceof ApiError && error.status === 401)) setError(message(error));
    }).finally(() => { if (!controller.signal.aborted) setReady(true); });
    return () => controller.abort();
  }, []);
  const signedOut = useCallback(() => { setUser(null); setError(''); }, []);
  if (!ready) return <main className="loading-screen" role="status"><Image src="/icon.svg" width={30} height={30} alt="" /><h1>RelayOps</h1><p>Opening your workspace…</p></main>;
  if (!user) return <Auth onLogin={setUser} initialError={error} />;
  return <Workspace key={user.id} user={user} onSignedOut={signedOut} />;
}

function Workspace({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  const [view, setView] = useState<View>('Overview');
  const [selected, setSelected] = useState<string | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [events, setEvents] = useState<RelayEvent[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextStats, nextEvents, nextSources, nextDestinations] = await Promise.all([
        apiFetch<Stats>('/api/stats', { signal }), apiFetch<RelayEvent[]>(`/api/events?limit=25&offset=${offset}`, { signal }),
        apiFetch<Source[]>('/api/sources', { signal }), apiFetch<Destination[]>('/api/destinations', { signal }),
      ]);
      if (!signal?.aborted) { setStats(nextStats); setEvents(nextEvents); setSources(nextSources); setDestinations(nextDestinations); setLoaded(true); setError(''); }
    } catch (error) {
      if (signal?.aborted) return;
      if (error instanceof ApiError && error.status === 401) { onSignedOut(); return; }
      setError(message(error));
    }
  }, [offset, onSignedOut]);
  useEffect(() => {
    const controller = new AbortController();
    queueMicrotask(() => { if (!controller.signal.aborted) void refresh(controller.signal); });
    const timer = setInterval(() => { void refresh(controller.signal); }, 10_000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [refresh]);
  const navigate = (next: View) => { setView(next); setSelected(null); setOffset(0); };
  const openEvent = (id: string) => { setView('Events'); setSelected(id); };
  const descriptions: Record<View, string> = {
    Overview: 'A clear view of every event, from receipt to delivery.', Events: 'Inspect incoming payloads and follow their delivery history.',
    Sources: 'Dedicated, protected endpoints for your webhook senders.', Destinations: 'Connect your events to the workflows that need them.',
  };
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><Image src="/icon.svg" width={29} height={29} alt="" /><span>RelayOps<small>WEBHOOK OPERATIONS</small></span></div><span className="nav-label">WORKSPACE</span><nav aria-label="Main navigation">{navigation.map(item => <Button key={item.name} variant="ghost" className={`nav-item ${view === item.name ? 'active' : ''}`} onClick={() => navigate(item.name)} aria-current={view === item.name ? 'page' : undefined}><item.icon size={18} /><span>{item.name}</span>{view === item.name && <i />}</Button>)}</nav>
      <div className="sidebar-note"><ShieldCheck size={22} /><h3>Received. Recorded. Retried.</h3><p>Your events have a history.<br />Your failures have a way forward.</p></div>
      <div className="profile"><div className="avatar">{user.email[0].toUpperCase()}</div><div><strong>My workspace</strong><small title={user.email}>{user.email}</small></div><Button variant="ghost" size="icon" aria-label="Sign out" disabled={busy} onClick={async () => {
        setBusy(true); try { await apiFetch('/api/auth/logout', { method: 'POST' }); onSignedOut(); } catch (error) { setError(message(error)); } finally { setBusy(false); }
      }}><LogOut size={17} /></Button></div>
    </aside>
    <div className="workspace"><header className="topbar"><span>Workspace <ChevronRight size={13} /><strong>{view}</strong>{selected && <><ChevronRight size={13} /><span>Event details</span></>}</span><span className="topbar-note"><span className="small-dot" />Refreshes every 10s</span></header>
      <main className="main-content">
        {!selected && <div className="page-heading"><div><span className="eyebrow">RELAYOPS / {view.toUpperCase()}</span><h1>{view === 'Overview' ? 'Every event, in view.' : view}</h1><p>{descriptions[view]}</p></div><Button variant="outline" disabled={busy} onClick={async () => { setBusy(true); await refresh(); setBusy(false); }}><RefreshCw size={15} />{busy ? 'Refreshing…' : 'Refresh'}</Button></div>}
        {error && <p className="error" role="alert">{error}</p>}
        {!loaded && !error && <p role="status">Loading your events and destinations…</p>}
        {selected ? <EventDetail key={selected} id={selected} destinations={destinations} onBack={() => setSelected(null)} onChange={refresh} /> : loaded && <>
          {view === 'Overview' && <>
            <div className="stats-grid">{[
              { label: 'Events received', value: stats?.events ?? 0, note: 'Persisted and accounted for', icon: Inbox },
              { label: 'Delivered', value: stats?.delivered ?? 0, note: stats?.success_rate === null ? 'No deliveries yet' : `${stats?.success_rate ?? 0}% of all delivery jobs`, icon: CheckCheck },
              { label: 'Pending', value: (stats?.queued ?? 0) + (stats?.running ?? 0), note: 'Queued or currently sending', icon: Activity },
              { label: 'Needs attention', value: stats?.failed ?? 0, note: 'Retry limit reached', icon: RefreshCw },
            ].map(stat => <Card key={stat.label} className="stat-card"><CardContent><div><span>{stat.label}</span><stat.icon size={17} /></div><strong>{stat.value.toLocaleString()}</strong><small>{stat.note}</small></CardContent></Card>)}</div>
            <Card className="routing-card"><CardContent><div><span className="eyebrow">YOUR DELIVERY PIPELINE</span><h2>A dependable handoff.</h2><p>{sources.length ? 'Events are stored before they enter the delivery queue.' : 'Connect your first source to start receiving webhooks.'}</p><Button variant="link" onClick={() => navigate(sources.length ? 'Destinations' : 'Sources')}>{sources.length ? 'Manage destinations' : 'Connect a source'}<ArrowRight size={15} /></Button></div><div className="pipeline"><div><Radio /><strong>{sources.filter(s => s.enabled).length} sources</strong><small>Receive</small></div><span className="pipeline-line" /><div><Workflow /><strong>RelayOps</strong><small>Persist & retry</small></div><span className="pipeline-line" /><div><Send /><strong>{destinations.filter(d => d.enabled).length} destinations</strong><small>Deliver</small></div></div></CardContent></Card>
            <section className="event-section"><div className="section-heading"><div><h2>Recent events</h2><p>The latest arrivals across your sources.</p></div><Button variant="ghost" onClick={() => navigate('Events')}>View all events<ArrowRight size={15} /></Button></div><Card className="table-card"><EventTable events={events.slice(0, 8)} onOpen={openEvent} /></Card></section>
            <div className="overview-footer"><ShieldCheck size={15} /><span>Events are persisted before acknowledgment. Failed deliveries retry automatically.</span><span>ALL-TIME TOTALS</span></div>
          </>}
          {view === 'Events' && <><Card className="table-card"><EventTable events={events} onOpen={openEvent} /></Card><div className="pagination"><span>{stats?.events ? `${offset + 1}–${offset + events.length} of ${stats.events} events` : '0 events'}</span><div><Button variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}><ChevronLeft size={15} />Previous</Button><Button variant="outline" disabled={offset + events.length >= (stats?.events ?? 0)} onClick={() => setOffset(offset + 25)}>Next<ChevronRight size={15} /></Button></div></div></>}
          {(view === 'Sources' || view === 'Destinations') && <Configuration key={view} mode={view} sources={sources} destinations={destinations} refresh={refresh} />}
        </>}
      </main><footer className="app-footer"><span>RelayOps <span className="muted">/</span> Webhook reliability</span><span>Receive with confidence. Deliver with visibility.</span></footer>
    </div>
  </div>;
}
