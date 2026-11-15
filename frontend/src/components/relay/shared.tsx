import { Inbox, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { dateTime, type RelayEvent, type Status } from '@/lib/relay';

export function Badge({ status }: { status: Status | 'enabled' | 'paused' }) {
  return <span className={`badge badge-${status}`}><span />{status}</span>;
}

export function EventTable({ events, onOpen }: { events: RelayEvent[]; onOpen: (id: string) => void }) {
  if (!events.length) return <div className="empty"><Inbox size={30} /><h3>No events yet</h3><p>Connect a source and destination, then send your first webhook.</p></div>;
  return <div className="table-scroll"><table><thead><tr><th>Event</th><th>Source</th><th>Received</th><th>Deliveries</th><th>Status</th><th><span className="sr-only">Inspect</span></th></tr></thead><tbody>{events.map(event => <tr key={event.id}>
    <td><Button variant="link" className="event-link" onClick={() => onOpen(event.id)} title={event.id}>{event.id.slice(0, 8)}<span className="muted">…</span></Button><small>{event.size_bytes.toLocaleString()} bytes</small></td>
    <td>{event.source_name}</td><td className="date-cell">{dateTime(event.received_at)}</td><td>{event.delivery_count}</td><td><Badge status={event.status} /></td><td><Button variant="ghost" size="icon" onClick={() => onOpen(event.id)} aria-label={`Inspect event ${event.id}`}><ChevronRight size={16} /></Button></td>
  </tr>)}</tbody></table></div>;
}
