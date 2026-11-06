"""Throwaway Docker sandbox runner (Blast Radius Zero).

Hardening defaults:
- --network none
- --read-only rootfs + tmpfs /tmp
- empty env (--env-file /dev/null + no host -e)
- --memory / --cpus / --ulimit
- --user non-root
- --rm (destroy after run)
- bind-mount only a job workdir at /work

Agent code runs in Docker. If the daemon is down, the runner refuses to
execute it on the host. ``FORCE_MOCK_SANDBOX=1`` is the offline stand-in
used by tests: hostile patterns are reported as blocked and never opened.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from csv_fixer.config import Settings
from csv_fixer.models import SandboxResult


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_docker_run_args(
    *,
    image: str,
    work_dir: Path,
    timeout_sec: int,
    memory: str,
    cpus: str,
    script_name: str = "fix.py",
    host_work_dir: Optional[str] = None,
    container_name: Optional[str] = None,
) -> list[str]:
    """Return the exact ``docker run`` argv used for containment (also asserted in tests)."""
    mount = host_work_dir or str(work_dir.resolve())
    args = [
        "docker",
        "run",
        "--rm",
    ]
    if container_name:
        args.extend(["--name", container_name])
    args.extend(
        [
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            cpus,
            "--pids-limit",
            "64",
            "--user",
            "65534:65534",  # nobody
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--env-file",
            "/dev/null",
            "-v",
            f"{mount}:/work:rw",
            "-w",
            "/work",
            image,
            "python",
            f"/work/{script_name}",
        ]
    )
    # timeout_sec is enforced by the runner (docker kill), not a docker flag.
    del timeout_sec
    return args


def container_name_for(job_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id).strip("-") or "job"
    return f"csvfix-{safe}"[:63]


class SandboxRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run_script(
        self,
        *,
        code: str,
        input_csv: Path,
        job_id: str,
        work_root: Optional[Path] = None,
    ) -> SandboxResult:
        work_root = work_root or Path(tempfile.mkdtemp(prefix=f"csvfix-{job_id}-"))
        work_root.mkdir(parents=True, exist_ok=True)
        # The sandbox user is 65534 and must be able to create output.csv.
        os.chmod(work_root, 0o777)
        script_path = work_root / "fix.py"
        in_path = work_root / "input.csv"
        out_path = work_root / "output.csv"
        script_path.write_text(code, encoding="utf-8")
        shutil.copyfile(input_csv, in_path)
        os.chmod(script_path, 0o644)
        os.chmod(in_path, 0o644)

        name = container_name_for(job_id)
        args = build_docker_run_args(
            image=self.settings.sandbox_image,
            work_dir=work_root,
            host_work_dir=self._host_mount(work_root),
            timeout_sec=self.settings.sandbox_timeout_sec,
            memory=self.settings.sandbox_memory,
            cpus=self.settings.sandbox_cpus,
            container_name=name,
        )

        if self.settings.force_mock_sandbox:
            return self._run_mock(code=code, work_root=work_root, args=args, out_path=out_path)

        if not docker_available():
            return SandboxResult(
                ok=False,
                exit_code=127,
                stdout="",
                stderr=(
                    "docker daemon unavailable; refusing to execute agent code on the host"
                ),
                timed_out=False,
                network_attempt_blocked=False,
                env_secret_attempt_blocked=False,
                output_csv_path=None,
                destroyed=True,
                docker_args=list(args),
                duration_ms=0,
            )

        return self._run_docker(
            args=args, out_path=out_path, code=code, container_name=name
        )

    def _host_mount(self, work_root: Path) -> str:
        """Path the host Docker daemon must bind.

        When the API runs in a container, ``jobs_dir`` is the in-container
        path and ``host_jobs_dir`` is the same directory on the host.
        """
        resolved = work_root.resolve()
        host_root = (self.settings.host_jobs_dir or "").strip()
        if not host_root:
            return str(resolved)
        jobs = Path(self.settings.jobs_dir).resolve()
        try:
            rel = resolved.relative_to(jobs)
        except ValueError:
            return str(resolved)
        return str(Path(host_root) / rel)

    def _run_docker(
        self,
        *,
        args: Sequence[str],
        out_path: Path,
        code: str,
        container_name: str,
    ) -> SandboxResult:
        t0 = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_timeout_sec + 5,
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            exit_code = 124
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        network_blocked = _looks_like_network_block(stdout, stderr) or re_search_net(code)
        env_blocked = _looks_like_empty_env(stdout, stderr) or re_search_env(code)
        produced = out_path.exists() and not timed_out and not network_blocked and not env_blocked
        ok = exit_code == 0 and produced
        return SandboxResult(
            ok=ok,
            exit_code=exit_code,
            stdout=_clip(stdout),
            stderr=_clip(stderr),
            timed_out=timed_out,
            network_attempt_blocked=network_blocked,
            env_secret_attempt_blocked=env_blocked,
            output_csv_path=str(out_path) if produced else None,
            destroyed=True,  # --rm
            docker_args=list(args),
            duration_ms=duration_ms,
        )

    def _run_mock(
        self,
        *,
        code: str,
        work_root: Path,
        args: list[str],
        out_path: Path,
    ) -> SandboxResult:
        """Offline stand-in when Docker is intentionally skipped.

        Does NOT execute hostile code. It statically detects network/env
        access and reports those as blocked, then runs a restricted local
        interpreter for repair code that does not match those patterns.
        Not a security boundary — real demos must use Docker.
        """
        t0 = time.perf_counter()
        networkish = bool(re_search_net(code))
        envish = bool(re_search_env(code))
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0

        if networkish:
            stdout_parts.append("BLOCKED NetworkError: network disabled (--network none)")
            exit_code = 42
        if envish:
            stdout_parts.append("ENV_DUMP {}")
            stdout_parts.append("SECRET_KEYS []")
            stdout_parts.append("BLOCKED env secrets empty by policy")
            exit_code = 42

        if networkish or envish:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            return SandboxResult(
                ok=False,
                exit_code=exit_code,
                stdout="\n".join(stdout_parts) + "\n",
                stderr="\n".join(stderr_parts),
                timed_out=False,
                network_attempt_blocked=networkish,
                env_secret_attempt_blocked=envish,
                output_csv_path=None,
                destroyed=True,
                docker_args=args,
                duration_ms=duration_ms,
            )

        # Safe path: run locally with near-empty env and cwd=work_root.
        # Rewrite /work/... paths so host execution mirrors the container layout.
        local_script = work_root / "fix.local.py"
        local_script.write_text(
            code.replace("/work/", str(work_root.resolve()) + "/"),
            encoding="utf-8",
        )
        bare_env = {"PATH": "/usr/bin:/bin", "HOME": str(work_root), "LANG": "C"}
        # Same interpreter as the control plane so the offline stand-in can import pandas.
        # The real sandbox image supplies its own interpreter; this path is mock-only.
        python = sys.executable or "python3"
        try:
            proc = subprocess.run(
                [python, str(local_script)],
                cwd=str(work_root),
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_timeout_sec,
                env=bare_env,
            )
            stdout = proc.stdout
            stderr = proc.stderr
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            return SandboxResult(
                ok=False,
                exit_code=124,
                stdout="",
                stderr="mock sandbox timeout",
                timed_out=True,
                destroyed=True,
                docker_args=args,
                duration_ms=duration_ms,
            )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        ok = exit_code == 0 and out_path.exists() and not timed_out
        return SandboxResult(
            ok=ok,
            exit_code=exit_code,
            stdout=_clip(stdout),
            stderr=_clip(stderr),
            network_attempt_blocked=False,
            env_secret_attempt_blocked=False,
            output_csv_path=str(out_path) if out_path.exists() else None,
            destroyed=True,
            docker_args=args,
            duration_ms=duration_ms,
        )


def re_search_net(code: str) -> bool:
    return bool(
        re.search(
            r"\b(socket\.|urllib|requests\.|httpx\.|subprocess\.(?:run|Popen|call).*curl|"
            r"create_connection|urlopen)\b",
            code,
        )
    )


def re_search_env(code: str) -> bool:
    return bool(re.search(r"\b(os\.environ|os\.getenv|environ\[)", code))


def _looks_like_network_block(stdout: str, stderr: str) -> bool:
    blob = (stdout + "\n" + stderr).lower()
    needles = (
        "network is unreachable",
        "name or service not known",
        "temporary failure in name resolution",
        "network disabled",
        "errno 101",
        "failed to establish a new connection",
        "nodename nor servname",
    )
    return any(n in blob for n in needles)


def _looks_like_empty_env(stdout: str, stderr: str) -> bool:
    blob = stdout + "\n" + stderr
    compact = blob.replace(" ", "")
    if "ENV_DUMP{}" in compact or "SECRET_KEYS[]" in compact:
        return True
    return "env secrets empty" in blob.lower()


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _clip(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"
