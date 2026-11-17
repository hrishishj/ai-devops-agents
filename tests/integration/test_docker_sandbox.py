"""Real-container tests: prove the isolation actually holds. Skipped when Docker or the image is missing.

docker build -t devopsagents-sandbox infra/sandbox
pytest tests/integration/test_docker_sandbox.py
"""

from __future__ import annotations

import os
import subprocess

import pytest

from devopsagents.integrations.sandbox.base import parse_pytest_outcomes
from devopsagents.integrations.sandbox.docker import DockerSandbox

IMAGE = os.environ.get("DA_SANDBOX_IMAGE", "devopsagents-sandbox")


def _ready() -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ready(), reason=f"docker or image {IMAGE!r} not available"),
]


def run(test_source: str, timeout: int = 60):
    return DockerSandbox(IMAGE).run_pytest({"t/test_x.py": test_source}, "t/test_x.py", timeout)


def outcomes(result):
    return parse_pytest_outcomes(result.stdout)


def test_a_normal_test_runs_and_passes():
    r = run("def test_ok():\n    assert 1 + 1 == 2\n")
    assert outcomes(r) == {"test_ok": "PASSED"} and r.exit_code == 0


def test_failures_are_reported_as_failures():
    assert outcomes(run("def test_bad():\n    assert 1 == 2\n")) == {"test_bad": "FAILED"}


def test_the_network_is_unreachable():
    src = (
        "import socket, pytest\n\n"
        "def test_network_is_blocked():\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
    )
    assert outcomes(run(src)) == {"test_network_is_blocked": "PASSED"}


def test_root_filesystem_is_read_only_but_tmp_is_writable():
    src = (
        "import pytest\n\n"
        "def test_cannot_write_outside_tmp():\n"
        "    with pytest.raises(OSError):\n"
        "        open('/etc/pwned', 'w')\n\n"
        "def test_tmp_is_writable(tmp_path):\n"
        "    (tmp_path / 'x').write_text('ok')\n"
    )
    assert outcomes(run(src)) == {"test_cannot_write_outside_tmp": "PASSED", "test_tmp_is_writable": "PASSED"}


def test_runs_as_a_non_root_user_with_no_host_secrets(monkeypatch):
    monkeypatch.setenv("DA_GITHUB_TOKEN", "super-secret-host-value")
    src = (
        "import os\n\n"
        "def test_not_root():\n"
        "    assert os.getuid() != 0\n\n"
        "def test_no_host_environment_leaks():\n"
        "    assert not any('DA_' in k or 'super-secret' in v for k, v in os.environ.items())\n"
    )
    assert outcomes(run(src)) == {"test_not_root": "PASSED", "test_no_host_environment_leaks": "PASSED"}


def test_a_hanging_test_is_stopped():
    r = run("import time\n\ndef test_hang():\n    time.sleep(60)\n", timeout=3)
    assert r.timed_out


def test_coverage_is_measured_inside_the_isolated_container():
    source = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    test = "from m import f\n\n\ndef test_f():\n    assert f(True) == 1\n"
    conftest = "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
    files = {"m.py": source, "t/test_x.py": test, "conftest.py": conftest}
    r = DockerSandbox(IMAGE).run_pytest(files, "t/test_x.py", 60, cover=["m.py"])
    assert outcomes(r) == {"test_f": "PASSED"}
    assert r.coverage is not None, "the image must include the coverage package"
    assert 4 in r.coverage["m.py"]["missing"]  # `return 2` was never executed
    assert {1, 2, 3} <= set(r.coverage["m.py"]["executed"])
