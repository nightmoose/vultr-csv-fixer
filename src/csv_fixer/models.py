"""Shared domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return uuid4().hex[:12]


class BlockReason(str, Enum):
    NETWORK = "network_denied"
    ENV_SECRET = "env_secret_denied"
    TIMEOUT = "timeout"
    OOM = "oom"
    CAPABILITY = "capability_denied"
    OTHER = "other"


class AuditEvent(BaseModel):
    ts: datetime = Field(default_factory=utcnow)
    job_id: str
    stage: str
    action: str
    detail: str = ""
    blocked: bool = False
    block_reason: Optional[BlockReason] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ViolationOut(BaseModel):
    field: str
    message: str
    kind: str


class ValidationReport(BaseModel):
    passed: bool
    violations: list[ViolationOut] = Field(default_factory=list)
    rows_checked: int = 0
    backend: str = "stub"


class SandboxResult(BaseModel):
    ok: bool
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    network_attempt_blocked: bool = False
    env_secret_attempt_blocked: bool = False
    output_csv_path: Optional[str] = None
    destroyed: bool = True
    docker_args: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class FixAttempt(BaseModel):
    attempt: int
    code: str
    sandbox: SandboxResult
    validation: Optional[ValidationReport] = None


class JobResult(BaseModel):
    job_id: str
    success: bool
    attempts: list[FixAttempt] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)
    fixed_csv_path: Optional[str] = None
    message: str = ""
