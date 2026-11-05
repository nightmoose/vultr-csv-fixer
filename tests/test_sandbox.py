from pathlib import Path

from csv_fixer.config import Settings
from csv_fixer.sandbox import SandboxRunner, build_docker_run_args, re_search_env, re_search_net

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
