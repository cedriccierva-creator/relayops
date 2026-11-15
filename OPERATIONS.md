# RelayOps operations

Run commands from `/path/to/relayops/backend` using the existing
`.venv`. API, worker and operator commands must share the same configuration and
absolute `DATABASE_URL`. Never print `.env` or credentials in incident logs.

## Start, stop and restart

After review, start the API in its own terminal:

```bash
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Start exactly one worker in another terminal:

```bash
.venv/bin/python worker.py
```

Stop each from its own terminal with Ctrl+C. The worker also accepts SIGTERM
sent to its verified PID; it finishes the current bounded attempt before exiting.
Do not launch a replacement until the old worker has exited. Do not use broad
process-kill commands. Default ports are 8000 for the API and 3000 for the
frontend. Choose unused ports and matching origins for custom installations.

For an approved restart: verify configuration and backup, stop the worker gracefully,
stop the API if its code/config changed, deploy the same revision, start the API,
then one worker. Settings are cached until process restart. Never delete the lock
file to bypass an exclusion error. Pending work survives restart; no DB reset is needed.

## Health and backlog

```bash
curl --fail http://127.0.0.1:8000/api/health
.venv/bin/python diagnostics.py
```

API health is only HTTP liveness. Diagnostics is a **local operator command**, not
a public endpoint. OS permissions on the database, its directory and worker lock
control access; restrict these and backups to the service/operator account.
It reads all owners' aggregate counts. It opens existing SQLite with `mode=ro`
and `query_only`, never creates tables or changes delivery state, and prints no payloads.
Exit codes: 0 healthy worker, 1 nonhealthy worker, 2 unavailable database/configuration.

Worker health checks the existing database-specific `.worker.lock`: an exclusive
lock must be held and its last successful-cycle timestamp must be fresh. Worker
startup clears the previous timestamp; successful cycles update at most once per
10 seconds. Exceptions do not refresh it. No database heartbeat writes or migration.
`WORKER_HEALTH_MAX_AGE_SECONDS` defaults to 120, bounded 15–3600. Override for a check:

```bash
WORKER_HEALTH_MAX_AGE_SECONDS=180 .venv/bin/python diagnostics.py
```

Statuses: `healthy`, `stale`, `starting` (no completed cycle), `stopped` (lock free),
`missing`, `unavailable` (lock access error), or `clock_skew` (future timestamp).
Use a threshold above delivery timeout plus the 10-second heartbeat interval and
storage delays; low thresholds can report false staleness during a normal attempt.
The default accommodates the maximum configured HTTP timeout of 60 seconds.

Healthy means the updated worker recently completed a recovery/claim/delivery
cycle, including an idle cycle or a handled failed delivery. It does **not** prove
successful downstream processing, queue drainage or n8n health. Lock and timestamp
checks are a point-in-time observation, not a process identity/security guarantee.
Clock jumps, filesystem stalls, external touching/replacing of the lock and using
the wrong database path can mislead. Workers from older revisions may not maintain the timestamp; verify the deployed
revision before relying on this signal. Do not remove/touch the lock.

Metrics describe persisted **jobs**, not events or attempts:

| Field | Meaning |
| --- | --- |
| `total_jobs` | All retained jobs, including terminal history |
| `queued`, `running` | Current waiting and claimed states |
| `delivered`, `failed` | Retained terminal-state totals; failed includes exhausted transient failures |
| `queued_due`, `queued_scheduled` | Queued subsets with due time at/before now or in the future |
| `outstanding` | queued + running, including disabled destinations |
| `oldest_outstanding_age_seconds` | Age since job creation, not time since last attempt; null when empty |
| `unknown_status` | Jobs outside the four supported statuses; investigate if nonzero |

Scheduled jobs include retry backoff; the schema has no separate retry status.
Due jobs are not necessarily eligible: disabled destinations and exhausted budgets
also block claims. Replayed jobs retain their original creation time. Counts are
one aggregate SQL snapshot with fixed output size; large databases still require
a scan and may briefly contend with writes.

## Delivery failures and replay

Use the existing signed-in UI or authenticated API: `GET /api/events?limit=50&offset=0`,
`GET /api/events/{event_id}`, and `GET /api/deliveries/{job_id}/attempts`.
`GET /api/stats` is authenticated and owner-scoped. Operator CLI counts are global.
Worker logs include event/job IDs, lifetime attempt number, HTTP code, safe error
category, transient/permanent classification and committed outcome. They omit URLs,
headers, payloads and response bodies. Avoid enabling HTTP client debug logs.

- **404:** permanent; confirm the configured destination path and n8n workflow
  activation with its operator. Correct through destination editing, then replay
  only after approval. Do not probe live n8n during an investigation without approval.
- **429:** transient delivery failure; retries use existing exponential backoff,
  not downstream Retry-After. Inbound webhook 429 is instead the API ingestion
  quota; providers should honor its Retry-After and reuse their idempotency key.
- **5xx / 408:** transient; bounded retries, then failed. Check downstream availability.
- **Timeout / DNS / connection / transport:** transient; check connectivity and
  `DELIVERY_TIMEOUT_SECONDS`. Timeout can occur after downstream acceptance.
- **Other 4xx, redirects, blocked destinations:** permanent. Never disable SSRF or
  redirect protections to make delivery succeed.

Manual replay is `POST /api/deliveries/{job_id}/retry` with the existing authenticated
session and allowed browser Origin. Prefer the UI's retry action. Only failed jobs
with enabled destinations can replay (otherwise 409); it resets the cycle budget,
preserves history and IDs, and uses the current destination URL. Inspect the failure,
fix its cause and confirm downstream idempotency first. No bulk replay is supported.

For stuck `running` jobs, check worker freshness and storage first. After lease
expiry (claim time + HTTP timeout + 60 seconds), the worker records
`worker_interrupted`, then queues with backoff from recovery time or marks failed
when attempts are exhausted. Restart does not immediately clear leases. Wait for
lease/backoff and inspect history; never manually clear claims or edit status SQL.

## Limits, retention and backups

Ingestion settings: `WEBHOOK_RATE_LIMIT=60`, `WEBHOOK_RATE_WINDOW_SECONDS=60`,
`WEBHOOK_RATE_MAX_SOURCES=10000`. Quotas are per-source, process-local fixed windows;
restart resets them and multiple API processes multiply capacity. They are not
global flood protection. `WEBHOOK_MAX_BYTES=1048576` and
`WEBHOOK_READ_TIMEOUT=10` bound authenticated ingestion bodies.

Read-only retention planning (prefer a restored backup for investigation):

```bash
DATABASE_URL=sqlite:////absolute/path/to/restored.db \
  RETENTION_ENABLED=true RETENTION_DAYS=30 RETENTION_BATCH_SIZE=100 \
  .venv/bin/python retention.py --dry-run
```

Planning excludes active jobs and unfinished attempts, reports one bounded batch
and `has_more`, and never deletes or frees disk. `--execute` is unsupported.
Monitor disk growth. Before maintenance, take a consistent SQLite backup using
SQLite's online backup facility, or stop API/worker during an approved maintenance
window and preserve the DB and any journal/WAL files together. Do not casually copy
an active DB alone. Restrict/encrypt backups and test restoration in an isolated
directory with a mock destination, never a live n8n URL. Do not restore over live data.

## Local database permissions

Older installations may have database files or directories created under a
permissive launch umask. Inspect existing paths before deployment. API `main.py`
and worker `worker.py` initialize the policy at entrypoint import,
before imports that initialize database directories or connections. The helper
reads the Linux `/proc/self/status` Umask field without mutation, then makes one
`os.umask(current | 0o077)` call. It never temporarily relaxes stricter bits.
`make_engine()` and subsequent database operations do not change the mask.
This is a permanent process-wide policy, also affecting new logs, lock files and
future backup files; no temporary permissive window is used around database I/O.
Normal new directories/files are 0700/0600, or stricter if the launcher requires it.
This startup implementation requires Linux with readable `/proc/self/status`;
it fails closed otherwise. Configure other platforms separately before porting.
Run entrypoint initialization before application threads; other code must not
concurrently change the process mask. Files created by the launcher/interpreter
before entrypoint import require a restrictive launcher umask too. Stricter masks
that deny owner access are preserved even if they prevent database initialization.

Existing paths are deliberately unchanged, including existing backup files and
SQLite sidecars. Subprocess tests of rollback-journal and WAL modes confirm that SQLite
creates journal/WAL/SHM files with mode 0644 for an existing 0644 database,
even after restrictive startup. SQLite derives these modes from the database;
the umask is not a substitute for migrating permissive existing databases.
A private new DB file inside an existing shared-writable directory
is not sufficient: another user may replace directory entries. Use a dedicated
service account, a private local data directory, and trusted ancestors. Symlinks,
ACLs, shared group membership, root access and copies outside this directory are
outside the umask guarantee. Payload encryption is not provided.

Manual migration for an existing installation (maintenance approval required;
these steps are not run automatically):

1. Identify the actual configured absolute SQLite path without printing `.env`.
   Inspect its ownership, links, ancestor permissions and ACLs, for example:
   `namei -l /path/to/relayops/backend/data/app.db` and, if available,
   `getfacl` on the file, data directory and ancestors. Resolve unexpected links
   and confirm the API/worker use the same service account before changing access.
2. Schedule a maintenance window; gracefully stop only this installation's worker
   and API. Do not change live permissions while they run. Take a consistent backup
   into a service-owned 0700 directory using `umask 077` in the backup process.
   Preserve recovery files; never delete journal/WAL/SHM files as hardening.
3. As the owning service account, for the default path, run:

   ```bash
   chmod 0700 /path/to/relayops/backend/data
   chmod 0600 /path/to/relayops/backend/data/app.db
   ```

   Apply 0600 individually to any existing `app.db-journal`, `app.db-wal`,
   `app.db-shm`, `app.db.worker.lock` and backups after verifying ownership and
   that each is a regular file, not a symlink. Use the actual database basename
   for custom installations. Do not recursively chmod repository/virtualenv files.
4. Remove unintended ACL grants/default ACLs and group/other write access on
   relevant ancestors after reviewing shared-directory needs. Relocate data to
   a dedicated private directory if ancestors cannot be made trustworthy; update
   API and worker configuration together. Group traversal permissions can expose
   data to group members, and group-writable ancestors permit entry replacement;
   inspect the actual permissions and ACLs for your installation.
5. Launch API and one worker with shell `umask 077` (or service-manager equivalent,
   such as systemd `UMask=0077`), using the updated code. Verify modes/ACLs again,
   check API/worker health, and restore-test the backup in isolation. External
   backup commands must set their own umask and must not overwrite permissive
   existing targets without explicitly correcting those targets' permissions.

Default ACLs may grant access despite a restrictive umask; inspect/remove unwanted
inherited ACLs on storage and backup directories. Secure backup encryption, copies,
and restore destinations separately. No application backup command is provided;
the regression test uses SQLite's backup API in the hardened application process.

## Incident constraints

Only one worker per **local SQLite database on one host**; no NFS, cross-host
workers or independent sending loops. Delivery is at least once, not exactly once:
acceptance followed by persistence failure can repeat. Downstream should deduplicate
`Idempotency-Key` (job UUID), or `X-RelayOps-Event-ID` at the appropriate event scope.
Retry exhaustion means success is not guaranteed.

During incidents do not reset the DB, delete records, perform retention cleanup,
remove lock files, rotate secrets casually, weaken SSRF, log secrets, launch extra
workers, or replay live workflows without explicit authorization. New infrastructure
and destructive repair require separate review. Test only against temporary databases
and loopback mock destinations; leave existing services and live records untouched.
