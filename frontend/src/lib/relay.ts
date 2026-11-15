export type User = { id: number; email: string };
export type Source = { id: string; name: string; enabled: boolean; created_at: string };
export type Destination = Source & { source_id: string; url: string };
export type Status = 'queued' | 'running' | 'delivered' | 'failed' | 'unrouted';
export type RelayEvent = { id: string; source_id: string; source_name: string; received_at: string; size_bytes: number; status: Status; delivery_count: number };
export type Job = { id: string; event_id: string; destination_id: string; status: Status; attempts_total: number; cycle_attempts: number; max_attempts: number; next_attempt_at: string };
export type EventDetail = RelayEvent & { payload: unknown; request_metadata: Record<string, unknown>; jobs: Job[] };
export type Attempt = { id: string; number: number; started_at: string; finished_at: string | null; status_code: number | null; error: string | null; duration_ms: number | null; response_metadata: Record<string, string> };
export type Stats = { events: number; deliveries: number; queued: number; running: number; delivered: number; failed: number; success_rate: number | null };
export const dateTime = (value: string) => new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
export const message = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Please try again.';

export function failureReason(attempt?: Attempt): string {
  if (!attempt?.error) return '';
  const reasons: Record<string, string> = {
    permanent_http_error: 'Permanent HTTP failure. Check the destination configuration before replaying.',
    http_error: 'The destination returned an unsuccessful HTTP response.',
    timeout: 'Network timeout: the destination did not respond in time.',
    connection_error: 'Network failure: a connection could not be completed.',
    dns_error: 'Network failure: destination DNS resolution failed.',
    transport_error: 'Network failure: the request could not be sent.',
    destination_blocked: 'Destination blocked by URL security policy.',
    redirect_blocked: 'Redirect blocked. Configure the final webhook URL directly.',
    unexpected_status: 'Unexpected HTTP status. Check the destination before replaying.',
    worker_interrupted: 'The worker stopped before recording an outcome; delivery may have occurred.',
  };
  return reasons[attempt.error] || 'Delivery failed; no further diagnostic information is available.';
}

export function deliveryState(job: Job, latest?: Attempt, destination?: Destination): string {
  if (job.status === 'delivered') return 'Successfully delivered';
  if (job.status === 'running') return 'Delivery in progress';
  if (job.status === 'queued') {
    if (destination?.enabled === false) return 'Paused destination';
    return job.cycle_attempts > 0 ? 'Retry scheduled' : 'Pending delivery';
  }
  if (job.status === 'failed') {
    if (latest?.error === 'permanent_http_error') return 'Permanent HTTP failure';
    if (latest?.error === 'destination_blocked') return 'Destination blocked';
    if (latest?.error === 'redirect_blocked') return 'Redirect blocked';
    if (latest?.error === 'unexpected_status') return 'Unexpected HTTP status';
    return job.cycle_attempts >= job.max_attempts ? 'Retry limit exhausted' : 'Delivery failed';
  }
  return 'Unrouted';
}
