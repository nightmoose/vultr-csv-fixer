import tempfile
from pathlib import Path

from csv_fixer.config import Settings
from csv_fixer.sandbox import SandboxRunner, build_docker_run_args, re_search_env, re_search_net

ROOT = Path(__file__).resolve().parents[1]

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def test_docker_args_enforce_containment(tmp_path):
    args = build_docker_run_args(
        image="csv-fixer-sandbox:local",
        work_dir=tmp_path,
        timeout_sec=30,
        memory="512m",
        cpus="1",
    )
    assert args[:3] == ["docker", "run", "--rm"]
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "--memory" in args and args[args.index("--memory") + 1] == "512m"
    assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
    assert "--env-file" in args and args[args.index("--env-file") + 1] == "/dev/null"
    assert "--user" in args and args[args.index("--user") + 1] == "65534:65534"
    assert "no-new-privileges" in args


def test_mock_sandbox_blocks_network_and_env(tmp_path):
    settings = Settings(force_mock_sandbox=True, mock_llm=True)
    runner = SandboxRunner(settings)
    code = (
        "import os, socket\n"
        "print('ENV_DUMP', dict(os.environ))\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
    )
    result = runner.run_script(
        code=code,
        input_csv=FIX / "clean_orders.csv",
        job_id="t-net",
        work_root=tmp_path / "work",
    )
    assert result.ok is False
    assert result.network_attempt_blocked is True
    assert result.env_secret_attempt_blocked is True
    assert result.destroyed is True
    assert "--network" in result.docker_args
    assert result.docker_args[result.docker_args.index("--network") + 1] == "none"


def test_pattern_helpers():
    assert re_search_net("socket.create_connection(('x', 1))")
    assert re_search_env("print(os.environ)")
    assert not re_search_net("print('hello')")


def test_host_jobs_dir_rewrites_bind_mount(tmp_path):
    settings = Settings(
        force_mock_sandbox=True,
        mock_llm=True,
        jobs_dir=str(tmp_path / "container-jobs"),
        host_jobs_dir=str(tmp_path / "host-jobs"),
    )
    work = tmp_path / "container-jobs" / "job1" / "attempt-1"
    runner = SandboxRunner(settings)
    result = runner.run_script(
        code="print('hi')\n",
        input_csv=FIX / "clean_orders.csv",
        job_id="job1",
        work_root=work,
    )
    mount = result.docker_args[result.docker_args.index("-v") + 1]
    assert mount.startswith(str((tmp_path / "host-jobs").resolve()))
    assert mount.endswith(":/work:rw")
    assert "job1" in mount and "attempt-1" in mount
    assert "--name" in result.docker_args


def test_refuses_host_execution_when_docker_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("csv_fixer.sandbox.docker_available", lambda: False)
    settings = Settings(force_mock_sandbox=False, mock_llm=True)
    runner = SandboxRunner(settings)
    work = tmp_path / "work"
    result = runner.run_script(
        code="open('/work/output.csv','w').write('leaked\\n')\n",
        input_csv=FIX / "clean_orders.csv",
        job_id="no-docker",
        work_root=work,
    )
    assert result.ok is False
    assert result.exit_code == 127
    assert "refusing" in result.stderr
    assert not (work / "output.csv").exists()


def test_sandbox_dockerfile_has_no_entrypoint():
    text = (ROOT / "sandbox_worker" / "Dockerfile").read_text(encoding="utf-8")
    instructions = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(line.upper().startswith("ENTRYPOINT") for line in instructions)


def test_mkdtemp_work_dir_is_world_writable(tmp_path):
    work = Path(tempfile.mkdtemp(prefix="csvfix-", dir=tmp_path))
    assert work.stat().st_mode & 0o777 == 0o700
    settings = Settings(
        force_mock_sandbox=True,
        mock_llm=True,
        jobs_dir=str(tmp_path / "jobs"),
    )
    SandboxRunner(settings).run_script(
        code="print('hi')\n",
        input_csv=FIX / "clean_orders.csv",
        job_id="perm",
        work_root=work,
    )
    assert work.stat().st_mode & 0o777 == 0o777
    assert (work / "fix.py").stat().st_mode & 0o777 == 0o644
    assert (work / "input.csv").stat().st_mode & 0o777 == 0o644


def test_compose_wires_model_sandbox_and_shared_work_path():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "env_file:" in text
    assert ".env" in text
    assert "VULTR_INFERENCE_MODEL:" in text
    assert "glm-5.3-flash" in text
    assert "VULTR_INFERENCE_API_KEY:" in text
    assert "SANDBOX_MEMORY:" in text
    assert "SANDBOX_TIMEOUT_SEC:" in text
    assert "SANDBOX_CPUS:" in text
    assert "/var/lib/csvfix-work" in text
    assert "/var/run/docker.sock:/var/run/docker.sock" in text
    assert "2375" not in text
    assert "2376" not in text


def test_mock_sandbox_timeout(tmp_path):
    settings = Settings(force_mock_sandbox=True, mock_llm=True, sandbox_timeout_sec=1)
    runner = SandboxRunner(settings)
    result = runner.run_script(
        code="import time\ntime.sleep(30)\n",
        input_csv=FIX / "clean_orders.csv",
        job_id="t-time",
        work_root=tmp_path / "work",
    )
    assert result.timed_out is True
    assert result.ok is False
    assert result.exit_code == 124
