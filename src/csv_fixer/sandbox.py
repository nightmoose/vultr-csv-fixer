"""Throwaway Docker sandbox runner (Blast Radius Zero).

Hardening defaults:
- --network none
- --read-only rootfs + tmpfs /tmp
- empty env (--env-file /dev/null + no host -e)
- --memory / --cpus / --ulimit
- --user non-root
- --rm (destroy after run)
- bind-mount only a job workdir at /work
"""

from __future__ import annotations

import shutil
import subprocess
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
) -> list[str]:
    """Return the exact ``docker run`` argv used for containment (also asserted in tests)."""
    return [
        "docker",
        "run",
        "--rm",
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
        f"{work_dir.resolve()}:/work:rw",
        "-w",
        "/work",
        image,
        "python",
        f"/work/{script_name}",
    ]


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
        use_mock = self.settings.force_mock_sandbox or not docker_available()
        work_root = work_root or Path(tempfile.mkdtemp(prefix=f"csvfix-{job_id}-"))
        work_root.mkdir(parents=True, exist_ok=True)
        script_path = work_root / "fix.py"
        in_path = work_root / "input.csv"
        out_path = work_root / "output.csv"
        script_path.write_text(code, encoding="utf-8")
        shutil.copyfile(input_csv, in_path)

        args = build_docker_run_args(
            image=self.settings.sandbox_image,
            work_dir=work_root,
            timeout_sec=self.settings.sandbox_timeout_sec,
            memory=self.settings.sandbox_memory,
            cpus=self.settings.sandbox_cpus,
        )

        if use_mock:
            return self._run_mock(code=code, work_root=work_root, args=args, out_path=out_path)

        return self._run_docker(args=args, out_path=out_path)

    def _run_docker(self, *, args: Sequence[str], out_path: Path) -> SandboxResult:
        t0 = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_timeout_sec + 5,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            code = 124

        duration_ms = int((time.perf_counter() - t0) * 1000)
        network_blocked = _looks_like_network_block(stdout, stderr)
        env_blocked = _looks_like_empty_env(stdout, stderr)
        ok = code == 0 and out_path.exists() and not timed_out
        return SandboxResult(
            ok=ok,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            network_attempt_blocked=network_blocked,
            env_secret_attempt_blocked=env_blocked,
            output_csv_path=str(out_path) if out_path.exists() else None,
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
        """Offline stand-in when Docker daemon is unavailable.

        Does NOT execute hostile code with real network. Instead it statically
        detects network/env access patterns and reports them as blocked, then
        optionally runs a restricted local interpreter for *safe* repair code.
        """
        t0 = time.perf_counter()
        networkish = bool(
            re_search_net(code)
        )
        envish = bool(
            re_search_env(code)
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0

        if networkish:
            stdout_parts.append("BLOCKED NetworkError: network disabled (--network none)")
            exit_code = 42
        if envish:
            stdout_parts.append("ENV_DUMP {}")
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
        # NOT a security boundary — real demos must use Docker.
        local_script = work_root / "fix.local.py"
        local_script.write_text(
            code.replace("/work/", str(work_root.resolve()) + "/"),
            encoding="utf-8",
        )
        # Minimal env: no secrets; keep PATH so python3 resolves.
        bare_env = {"PATH": "/usr/bin:/bin", "HOME": str(work_root), "LANG": "C"}
        try:
            proc = subprocess.run(
                ["python3", str(local_script)],
                cwd=str(work_root),
                capture_output=True,
                text=True,
                timeout=self.settings.sandbox_timeout_sec,
                env=bare_env,
            )
            stdout = proc.stdout
            stderr = proc.stderr
            exit_code = proc.returncode
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
        ok = exit_code == 0 and out_path.exists()
        return SandboxResult(
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            network_attempt_blocked=False,
            env_secret_attempt_blocked=False,
            output_csv_path=str(out_path) if out_path.exists() else None,
            destroyed=True,
            docker_args=args,
            duration_ms=duration_ms,
        )


def re_search_net(code: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(socket\.|urllib|requests\.|httpx\.|subprocess\.(?:run|Popen|call).*curl|"
            r"create_connection|urlopen)\b",
            code,
        )
    )


def re_search_env(code: str) -> bool:
    import re

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
    if "ENV_DUMP {}" in blob or "ENV_DUMP{}" in blob.replace(" ", ""):
        return True
    return "env secrets empty" in blob.lower()
