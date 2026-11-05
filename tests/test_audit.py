from csv_fixer.audit import AuditLog
from csv_fixer.models import BlockReason


def test_audit_filters():
    log = AuditLog()
    log.record(job_id="a", stage="s", action="ok")
    log.record(
        job_id="a",
        stage="sandbox",
        action="block_network",
        blocked=True,
        block_reason=BlockReason.NETWORK,
    )
    log.record(job_id="b", stage="s", action="other")
    assert len(log.for_job("a")) == 2
    assert len(log.blocked_for_job("a")) == 1
    assert log.blocked_for_job("a")[0].block_reason == BlockReason.NETWORK
