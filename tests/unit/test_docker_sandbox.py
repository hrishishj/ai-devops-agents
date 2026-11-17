from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import SecretStr

from devopsagents.apps.container import build_container
from devopsagents.core.config.settings import Settings
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.sandbox.azure_sessions import AzureSessionsSandbox
from devopsagents.integrations.sandbox.base import SandboxUnavailable, parse_pytest_outcomes
from devopsagents.integrations.sandbox.docker import DockerSandbox

RESULT = '@@RESULT@@{"code": 0, "stdout": "PASSED t.py::test_a\\nFAILED t.py::test_b", "stderr": "", "timeout": false}'


class FakeRun:
    """Stands in for subprocess.run; records every call."""

    def __init__(self, stdout=RESULT, stderr="", returncode=0, raises=None):
        self.calls, self.stdout, self.stderr, self.rc, self.raises = [], stdout, stderr, returncode, raises

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.raises and argv[1] == "run":
            raise self.raises
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def sandbox(run, **kw):
    return DockerSandbox("my-image", run=run, **kw)


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


# ---- the isolation contract -----------------------------------------------------------------------
def test_container_is_locked_down_by_construction():
    run = FakeRun()
    sandbox(run, memory="256m", cpus="0.5", pids_limit=64).run_pytest({"a.py": "x"}, "t.py", 30)
    argv, _ = run.calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert flag_value(argv, "--network") == "none"
    assert "--read-only" in argv
    assert flag_value(argv, "--cap-drop") == "ALL"
    assert flag_value(argv, "--security-opt") == "no-new-privileges"
    assert flag_value(argv, "--user") == "65534:65534"  # non-root
    assert flag_value(argv, "--memory") == "256m" and flag_value(argv, "--memory-swap") == "256m"
    assert flag_value(argv, "--cpus") == "0.5" and flag_value(argv, "--pids-limit") == "64"
    assert flag_value(argv, "--tmpfs").startswith("/tmp:")
    assert argv[argv.index("my-image") + 1 : argv.index("my-image") + 3] == ["python", "-c"]


def test_no_host_access_of_any_kind_is_granted():
    run = FakeRun()
    sandbox(run).run_pytest({"a.py": "x"}, "t.py")
    argv, _ = run.calls[0]
    docker_options = argv[
        : argv.index("my-image")
    ]  # docker's own flags; the runner script comes after the image
    forbidden = {
        "-v",
        "--volume",
        "--mount",
        "--privileged",
        "--pid",
        "--ipc",
        "--uts",
        "--userns",
        "--add-host",
        "--device",
        "-p",
        "--publish",
        "-P",
        "--cap-add",
        "--env-file",
        "--volumes-from",
    }
    assert not forbidden & set(docker_options)
    assert "host" not in docker_options  # no --network host / --pid host
    assert not any("docker.sock" in a or a.startswith("/var/run") for a in docker_options)
    env_args = [docker_options[k + 1] for k, a in enumerate(docker_options) if a == "-e"]
    assert all(
        "=" in e and not any(w in e.upper() for w in ("TOKEN", "SECRET", "KEY", "PASSWORD")) for e in env_args
    )


def test_files_travel_over_stdin_not_via_the_filesystem():
    run = FakeRun()
    sandbox(run).run_pytest({"src/pkg/a.py": "SECRET_FREE"}, "gen/test_x.py", 45)
    argv, kw = run.calls[0]
    payload = json.loads(kw["input"])
    assert payload == {
        "files": {"src/pkg/a.py": "SECRET_FREE"},
        "target": "gen/test_x.py",
        "timeout": 45,
        "cover": [],
    }
    assert "SECRET_FREE" not in " ".join(argv)  # never on the command line


# ---- results and failures -------------------------------------------------------------------------
def test_parses_the_marked_result_line():
    r = sandbox(FakeRun()).run_pytest({}, "t.py")
    assert r.exit_code == 0 and not r.timed_out
    assert parse_pytest_outcomes(r.stdout) == {"test_a": "PASSED", "test_b": "FAILED"}


def test_timeout_kills_the_named_container_and_reports_timed_out():
    run = FakeRun(raises=subprocess.TimeoutExpired(cmd="docker", timeout=1))
    r = sandbox(run).run_pytest({}, "t.py", 1)
    assert r.timed_out
    run_argv, kill_argv = run.calls[0][0], run.calls[1][0]
    name = flag_value(run_argv, "--name")
    assert kill_argv == ["docker", "kill", name] and name.startswith("dasbx-")


def test_missing_docker_image_and_daemon_are_reported_clearly():
    with pytest.raises(SandboxUnavailable, match="docker build -t devopsagents-sandbox"):
        sandbox(FakeRun("", "Unable to find image 'my-image:latest' locally", 125)).run_pytest({}, "t.py")
    with pytest.raises(SandboxUnavailable, match="daemon is not running"):
        sandbox(
            FakeRun("", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", 125)
        ).run_pytest({}, "t.py")
    with pytest.raises(SandboxUnavailable, match="not installed"):
        sandbox(FakeRun(raises=FileNotFoundError())).run_pytest({}, "t.py")
    with pytest.raises(SandboxUnavailable, match="docker run failed"):
        sandbox(FakeRun("", "boom", 1)).run_pytest({}, "t.py")


def test_missing_pytest_in_the_image_is_unavailable_not_a_failure():
    out = '@@RESULT@@{"code": 1, "stdout": "", "stderr": "No module named pytest", "timeout": false}'
    with pytest.raises(SandboxUnavailable, match="pytest"):
        sandbox(FakeRun(out)).run_pytest({}, "t.py")
    with pytest.raises(SandboxUnavailable):
        sandbox(FakeRun("garbage", "", 0)).run_pytest({}, "t.py")


# ---- selection ------------------------------------------------------------------------------------
def kind_of(repo_root, **kw):
    c = build_container(
        Settings(database_url="sqlite+pysqlite://", github_token=SecretStr("t"), **kw), base_dir=repo_root
    )
    tg = c.handlers[AgentKind.SECURITY].__self__._testgen
    return None if tg is None else type(tg._sandbox)


def test_sandbox_selection_never_falls_back_to_a_local_runner(repo_root):
    assert kind_of(repo_root) is None  # auto, nothing configured -> feature off
    assert kind_of(repo_root, sandbox_kind="docker") is DockerSandbox
    assert kind_of(repo_root, sandbox_endpoint="https://pool.example") is AzureSessionsSandbox  # auto
    assert kind_of(repo_root, sandbox_kind="none", sandbox_endpoint="https://pool.example") is None
    assert kind_of(repo_root, sandbox_kind="azure_sessions") is None  # azure chosen but no endpoint
