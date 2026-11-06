"""Agent loop: propose fix → sandbox → DataContractGate validate → retry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from csv_fixer.audit import AuditLog, GLOBAL_AUDIT
from csv_fixer.config import Settings, load_settings
from csv_fixer.llm import LLMClient, sample_csv_text
from csv_fixer.models import (
    BlockReason,
    FixAttempt,
    JobResult,
    SandboxResult,
    ValidationReport,
    ViolationOut,
    new_job_id,
)
from csv_fixer.sandbox import SandboxRunner
from csv_fixer.validators import validate_csv_against_contract


class CsvFixAgent:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        audit: Optional[AuditLog] = None,
        llm: Optional[LLMClient] = None,
        sandbox: Optional[SandboxRunner] = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.audit = audit or GLOBAL_AUDIT
        self.llm = llm or LLMClient(self.settings)
        self.sandbox = sandbox or SandboxRunner(self.settings)

    def run(
        self,
        *,
        csv_path: Path | str,
        contract_path: Path | str,
        job_id: Optional[str] = None,
        work_root: Optional[Path] = None,
    ) -> JobResult:
        csv_path = Path(csv_path)
        contract_path = Path(contract_path)
        job_id = job_id or new_job_id()
        contract_yaml = contract_path.read_text(encoding="utf-8")
        csv_sample = sample_csv_text(str(csv_path))

        self.audit.record(
            job_id=job_id,
            stage="ingest",
            action="accepted",
            detail=f"csv={csv_path.name} contract={contract_path.name}",
        )

        attempts: list[FixAttempt] = []
        previous_errors: Optional[str] = None
        fixed_path: Optional[str] = None

        for i in range(1, self.settings.max_agent_retries + 1):
            self.audit.record(
                job_id=job_id,
                stage="llm",
                action="propose_fix",
                detail=f"attempt={i}",
                mock=self.settings.mock_llm,
            )
            try:
                code = self.llm.propose_fix_code(
                    csv_sample=csv_sample,
                    contract_yaml=contract_yaml,
                    previous_errors=previous_errors,
                )
            except Exception as exc:
                detail = _public_error(exc)
                self.audit.record(
                    job_id=job_id,
                    stage="llm",
                    action="llm_error",
                    detail=detail,
                )
                sand = SandboxResult(
                    ok=False,
                    exit_code=-1,
                    stderr=detail,
                    destroyed=True,
                )
                attempts.append(FixAttempt(attempt=i, code="", sandbox=sand, validation=None))
                previous_errors = detail
                continue

            self.audit.record(
                job_id=job_id,
                stage="sandbox",
                action="containment_policy",
                detail=(
                    f"attempt={i}; network=none env=empty "
                    f"memory={self.settings.sandbox_memory} "
                    f"cpus={self.settings.sandbox_cpus} user=65534 "
                    f"cap-drop=ALL timeout={self.settings.sandbox_timeout_sec}s rm=true"
                ),
            )

            sand = self.sandbox.run_script(
                code=code,
                input_csv=csv_path,
                job_id=f"{job_id}-{i}",
                work_root=(work_root / f"attempt-{i}") if work_root else None,
            )
            record_sandbox_events(
                self.audit, job_id, sand, attempt=i, settings=self.settings
            )

            validation = None
            if sand.ok and sand.output_csv_path:
                try:
                    validation = validate_csv_against_contract(
                        sand.output_csv_path, contract_path
                    )
                except Exception as exc:
                    validation = ValidationReport(
                        passed=False,
                        violations=[
                            ViolationOut(
                                field="",
                                message=_public_error(exc),
                                kind="validator_error",
                            )
                        ],
                        rows_checked=0,
                        backend="error",
                    )
                self.audit.record(
                    job_id=job_id,
                    stage="validate",
                    action="contract_check",
                    detail=f"passed={validation.passed} backend={validation.backend}",
                    violations=len(validation.violations),
                )
                if validation.passed:
                    attempts.append(
                        FixAttempt(attempt=i, code=code, sandbox=sand, validation=validation)
                    )
                    fixed_path = sand.output_csv_path
                    self.audit.record(
                        job_id=job_id,
                        stage="complete",
                        action="success",
                        detail=f"fixed file ready after {i} attempt(s)",
                    )
                    return JobResult(
                        job_id=job_id,
                        success=True,
                        attempts=attempts,
                        audit=self.audit.for_job(job_id),
                        fixed_csv_path=fixed_path,
                        message="fixed CSV passed contract validators",
                    )
                previous_errors = _format_validation_errors(validation)
            else:
                previous_errors = (
                    f"sandbox exit={sand.exit_code}\nstdout:\n{sand.stdout}\n"
                    f"stderr:\n{sand.stderr}"
                )

            attempts.append(
                FixAttempt(attempt=i, code=code, sandbox=sand, validation=validation)
            )

        self.audit.record(
            job_id=job_id,
            stage="complete",
            action="failed",
            detail=f"exhausted {self.settings.max_agent_retries} attempts",
        )
        return JobResult(
            job_id=job_id,
            success=False,
            attempts=attempts,
            audit=self.audit.for_job(job_id),
            fixed_csv_path=None,
            message="agent failed to produce a contract-valid CSV",
        )


def record_sandbox_events(
    audit: AuditLog,
    job_id: str,
    sand: SandboxResult,
    *,
    attempt: int,
    settings: Settings,
) -> None:
    """Write the containment audit rows for one sandbox run."""
    if sand.network_attempt_blocked:
        audit.record(
            job_id=job_id,
            stage="sandbox",
            action="block_network",
            detail=(
                f"attempt={attempt}; generated code attempted network access; "
                "--network none"
            ),
            blocked=True,
            block_reason=BlockReason.NETWORK,
            exit_code=sand.exit_code,
        )
    if sand.env_secret_attempt_blocked:
        audit.record(
            job_id=job_id,
            stage="sandbox",
            action="block_env",
            detail=(
                f"attempt={attempt}; generated code attempted env/secret read; "
                "empty env enforced"
            ),
            blocked=True,
            block_reason=BlockReason.ENV_SECRET,
            exit_code=sand.exit_code,
        )
    if sand.timed_out:
        audit.record(
            job_id=job_id,
            stage="sandbox",
            action="timeout",
            detail=f"attempt={attempt}; killed after {settings.sandbox_timeout_sec}s",
            blocked=True,
            block_reason=BlockReason.TIMEOUT,
        )
    blob = (sand.stdout + "\n" + sand.stderr).lower()
    if not sand.timed_out and (sand.exit_code == 137 or "out of memory" in blob):
        audit.record(
            job_id=job_id,
            stage="sandbox",
            action="block_oom",
            detail=f"attempt={attempt}; memory limit {settings.sandbox_memory}",
            blocked=True,
            block_reason=BlockReason.OOM,
            exit_code=sand.exit_code,
        )
    if sand.exit_code == 127 and "docker daemon unavailable" in sand.stderr:
        audit.record(
            job_id=job_id,
            stage="sandbox",
            action="sandbox_unavailable",
            detail=f"attempt={attempt}; {sand.stderr}",
        )

    audit.record(
        job_id=job_id,
        stage="sandbox",
        action="run_complete",
        detail=f"attempt={attempt}; exit={sand.exit_code} destroyed={sand.destroyed}",
        duration_ms=sand.duration_ms,
    )


def _format_validation_errors(report: ValidationReport) -> str:
    lines = [f"validation passed={report.passed} rows={report.rows_checked}"]
    for v in report.violations[:50]:
        lines.append(f"- {v.kind} {v.field}: {v.message}")
    return "\n".join(lines)


def _public_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", text, flags=re.I)
    return text[:500]
