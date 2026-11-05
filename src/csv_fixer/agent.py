"""Agent loop: propose fix → sandbox → DataContractGate validate → retry."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from csv_fixer.audit import AuditLog, GLOBAL_AUDIT
from csv_fixer.config import Settings, load_settings
from csv_fixer.llm import LLMClient, sample_csv_text
from csv_fixer.models import BlockReason, FixAttempt, JobResult, new_job_id
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
            code = self.llm.propose_fix_code(
                csv_sample=csv_sample,
                contract_yaml=contract_yaml,
                previous_errors=previous_errors,
            )

            sand = self.sandbox.run_script(
                code=code,
                input_csv=csv_path,
                job_id=f"{job_id}-{i}",
                work_root=(work_root / f"attempt-{i}") if work_root else None,
            )

            if sand.network_attempt_blocked:
                self.audit.record(
                    job_id=job_id,
                    stage="sandbox",
                    action="block_network",
                    detail="generated code attempted network access; --network none",
                    blocked=True,
                    block_reason=BlockReason.NETWORK,
                    exit_code=sand.exit_code,
                )
            if sand.env_secret_attempt_blocked:
                self.audit.record(
                    job_id=job_id,
                    stage="sandbox",
                    action="block_env",
                    detail="generated code attempted env/secret read; empty env enforced",
                    blocked=True,
                    block_reason=BlockReason.ENV_SECRET,
                    exit_code=sand.exit_code,
                )
            if sand.timed_out:
                self.audit.record(
                    job_id=job_id,
                    stage="sandbox",
                    action="timeout",
                    detail=f"killed after {self.settings.sandbox_timeout_sec}s",
                    blocked=True,
                    block_reason=BlockReason.TIMEOUT,
                )

            self.audit.record(
                job_id=job_id,
                stage="sandbox",
                action="run_complete",
                detail=f"exit={sand.exit_code} destroyed={sand.destroyed}",
                duration_ms=sand.duration_ms,
            )

            validation = None
            if sand.ok and sand.output_csv_path:
                validation = validate_csv_against_contract(
                    sand.output_csv_path, contract_path
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


def _format_validation_errors(report) -> str:
    lines = [f"validation passed={report.passed} rows={report.rows_checked}"]
    for v in report.violations[:50]:
        lines.append(f"- {v.kind} {v.field}: {v.message}")
    return "\n".join(lines)
