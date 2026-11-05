"""In-memory audit log for containment events."""

from __future__ import annotations

from typing import Iterable, Optional

from csv_fixer.models import AuditEvent, BlockReason


class AuditLog:
    """Append-only audit trail keyed by job_id."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(
        self,
        *,
        job_id: str,
        stage: str,
        action: str,
        detail: str = "",
        blocked: bool = False,
        block_reason: Optional[BlockReason] = None,
        **meta: object,
    ) -> AuditEvent:
        event = AuditEvent(
            job_id=job_id,
            stage=stage,
            action=action,
            detail=detail,
            blocked=blocked,
            block_reason=block_reason,
            meta=dict(meta),
        )
        self._events.append(event)
        return event

    def for_job(self, job_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.job_id == job_id]

    def blocked_for_job(self, job_id: str) -> list[AuditEvent]:
        return [e for e in self.for_job(job_id) if e.blocked]

    def all(self) -> list[AuditEvent]:
        return list(self._events)

    def extend(self, events: Iterable[AuditEvent]) -> None:
        self._events.extend(events)


# Process-wide default used by API/CLI unless a caller injects its own.
GLOBAL_AUDIT = AuditLog()
