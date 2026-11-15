"""Read-only retention planning. No cleanup until payload tombstones support replay."""
from datetime import timedelta

from sqlalchemy import exists, select

from app.models import DeliveryAttempt, DeliveryJob, ReceivedEvent, utcnow


def plan_retention(factory, settings, now=None):
    cutoff = (now or utcnow()) - timedelta(days=settings.RETENTION_DAYS)
    report = {"enabled": settings.RETENTION_ENABLED, "dry_run": True,
              "cleanup_supported": False, "cutoff": cutoff.isoformat(), "events": [],
              "candidate_bytes": 0, "has_more": False}
    if not settings.RETENTION_ENABLED:
        return report
    unsafe_job = exists(select(DeliveryJob.id).where(
        DeliveryJob.event_id == ReceivedEvent.id,
        (DeliveryJob.status.not_in(("delivered", "failed")))
        | DeliveryJob.claim_token.is_not(None) | DeliveryJob.lease_until.is_not(None),
    ))
    unfinished_attempt = exists(select(DeliveryAttempt.id).join(
        DeliveryJob, DeliveryAttempt.job_id == DeliveryJob.id,
    ).where(DeliveryJob.event_id == ReceivedEvent.id, DeliveryAttempt.finished_at.is_(None)))
    # Project metadata only: payloads are never loaded or printed. No writes, even
    # for terminal jobs: they may be manually replayed immediately after this plan.
    with factory() as db, db.begin():
        rows = db.execute(select(ReceivedEvent.id, ReceivedEvent.size_bytes).where(
            ReceivedEvent.received_at < cutoff, ~unsafe_job, ~unfinished_attempt,
        ).order_by(ReceivedEvent.received_at, ReceivedEvent.id)
          .limit(settings.RETENTION_BATCH_SIZE + 1)).all()
    report["has_more"] = len(rows) > settings.RETENTION_BATCH_SIZE
    report["events"] = [{"event_id": row.id, "size_bytes": row.size_bytes}
                        for row in rows[:settings.RETENTION_BATCH_SIZE]]
    report["candidate_bytes"] = sum(row["size_bytes"] for row in report["events"])
    return report
