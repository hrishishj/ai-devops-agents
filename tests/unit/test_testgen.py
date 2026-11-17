from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from devopsagents.agents.security.testgen import (
    TestGenerator,
    find_targets,
    guard,
    import_path,
    split_tests,
)
from devopsagents.agents.security.workflow import SecurityAgent
from devopsagents.core.jobs.runner import run_job
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.client import PullRequestInfo
from devopsagents.integrations.sandbox.azure_sessions import AzureSessionsSandbox
from devopsagents.integrations.sandbox.base import (
    SandboxResult,
    SandboxUnavailable,
    parse_pytest_outcomes,
)
from devopsagents.integrations.scanners.base import SecretsScanner

SOURCE = """def add(a, b):
    return a + b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def _private(x):
    return x
"""

GOOD_TESTS = """import pytest
from pkg.calc import add, divide


def test_add_happy():
    assert add(2, 3) == 5


def test_divide_by_zero_raises():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_wrong_expectation():
    assert add(2, 2) == 5
"""


class LocalTestSandbox:
    """TEST-ONLY stand-in that runs pytest in a temp dir. The real adapter must be isolated;
    nothing like this ships in src/."""

    def __init__(self) -> None:
        self.runs: list[list[str]] = []

    def run_pytest(self, files, target, timeout_seconds=60):
        with tempfile.TemporaryDirectory() as d:
            for rel, content in files.items():
                p = Path(d, rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-rA", "--tb=short", "-p", "no:cacheprovider", target],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            self.runs.append(sorted(parse_pytest_outcomes(r.stdout)))
            return SandboxResult(r.returncode, r.stdout, r.stderr)


def generator(store, repo_root, replies, sandbox=None):
    llm = LLMGateway(ScriptedBackend(replies=replies), store, sleep=lambda s: None)
    sb = sandbox or LocalTestSandbox()
    return TestGenerator(llm, PromptRegistry(repo_root / "prompts"), sb), sb


# ---- target selection --------------------------------------------------------------------------
def test_import_path():
    assert import_path("src/pkg/calc.py") == "pkg.calc"
    assert import_path("app/util.py") == "app.util"
    assert import_path("pkg/__init__.py") == "pkg"


def test_find_targets_uses_added_lines_and_skips_private_and_tested():
    added = {"src/pkg/calc.py": {2, 6}}  # inside add() and divide()
    t = find_targets({"src/pkg/calc.py": SOURCE}, added, "", max_functions=5, max_files=3)
    assert t[0].functions == ["add", "divide"]
    only_divide = find_targets({"src/pkg/calc.py": SOURCE}, added, "def test_add():", 5, 3)
    assert only_divide[0].functions == ["divide"]
    assert find_targets({"src/pkg/calc.py": SOURCE}, {"src/pkg/calc.py": {11}}, "", 5, 3) == []  # _private
    assert find_targets({"src/pkg/calc.py": SOURCE}, added, "", max_functions=1, max_files=3)[
        0
    ].functions == ["add"]
    assert (
        find_targets({"tests/test_x.py": SOURCE, "README.md": "x"}, {"tests/test_x.py": {2}}, "", 5, 3) == []
    )


# ---- guard -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code",
    [
        "import os\ndef test_a(): pass",
        "from subprocess import run\ndef test_a(): pass",
        "def test_a():\n    eval('1')",
        "def test_a():\n    open('/etc/passwd')",
        "import requests\ndef test_a(): pass",
        "class TestX:\n    def test_a(self): pass",
        "def test_a(:",
    ],
)
def test_guard_rejects_dangerous_or_unsupported_code(code):
    assert guard(code) is not None


def test_guard_accepts_plain_pytest():
    assert guard(GOOD_TESTS) is None


def test_split_keeps_decorators_and_header():
    code = "import pytest\n\n@pytest.mark.parametrize('x', [1])\ndef test_p(x):\n    assert x\n\n\ndef helper():\n    return 1\n"
    header, tests = split_tests(code)
    assert "@pytest.mark.parametrize" in tests["test_p"] and "helper" in header and "import pytest" in header


# ---- verification ------------------------------------------------------------------------------
def test_only_tests_that_pass_in_the_sandbox_are_kept(store, repo_root):
    gen, sandbox = generator(store, repo_root, [{"code": GOOD_TESTS}])
    target = find_targets({"src/pkg/calc.py": SOURCE}, {"src/pkg/calc.py": {2, 6}}, "", 5, 3)
    report = gen.run(target, "o/r")
    v = report.verified[0]
    assert v.test_names == ["test_add_happy", "test_divide_by_zero_raises"]
    assert v.dropped == 1 and "test_wrong_expectation" not in v.code
    assert len(sandbox.runs) == 2  # full run, then re-run of the surviving subset


def test_nothing_passes_means_nothing_reported(store, repo_root):
    bad = "from pkg.calc import add\n\n\ndef test_x():\n    assert add(1, 1) == 3\n"
    gen, _ = generator(store, repo_root, [{"code": bad}])
    target = find_targets({"src/pkg/calc.py": SOURCE}, {"src/pkg/calc.py": {2}}, "", 5, 3)
    report = gen.run(target, "o/r")
    assert report.verified == [] and "none of the generated tests passed" in report.notes[0]


def test_dangerous_generated_code_never_reaches_the_sandbox(store, repo_root):
    class Boom:
        def run_pytest(self, *a, **k):
            raise AssertionError("must not be called")

    gen, _ = generator(store, repo_root, [{"code": "import subprocess\ndef test_a(): pass"}], Boom())
    target = find_targets({"src/pkg/calc.py": SOURCE}, {"src/pkg/calc.py": {2}}, "", 5, 3)
    report = gen.run(target, "o/r")
    assert report.verified == [] and "banned import subprocess" in report.notes[0]


def test_unavailable_sandbox_degrades_gracefully(store, repo_root):
    class Down:
        def run_pytest(self, *a, **k):
            raise SandboxUnavailable("no pytest")

    gen, _ = generator(store, repo_root, [{"code": GOOD_TESTS}], Down())
    target = find_targets({"src/pkg/calc.py": SOURCE}, {"src/pkg/calc.py": {2}}, "", 5, 3)
    report = gen.run(target, "o/r")
    assert report.verified == [] and "sandbox unavailable" in report.notes[0]


def test_prompt_marks_source_as_untrusted_and_gives_import_path(store, repo_root):
    gen, _ = generator(store, repo_root, [{"code": GOOD_TESTS}])
    backend = gen._llm._backend
    gen.run(find_targets({"src/pkg/calc.py": SOURCE}, {"src/pkg/calc.py": {2}}, "", 5, 3), "o/r")
    assert "UNTRUSTED" in backend.calls[0]["system"]
    assert "<import_path>pkg.calc</import_path>" in backend.calls[0]["user"]


# ---- workflow integration ---------------------------------------------------------------------
DIFF = (
    """diff --git a/src/pkg/calc.py b/src/pkg/calc.py
new file mode 100644
--- /dev/null
+++ b/src/pkg/calc.py
@@ -0,0 +1,11 @@
"""
    + "\n".join("+" + line for line in SOURCE.splitlines())
    + "\n"
)


class GH:
    def __init__(self, cfg):
        self.cfg, self.comments, self.checks = cfg, [], []

    def get_pr(self, inst, repo, number):
        return PullRequestInfo(repo, number, "t", "b", "sha1", "base")

    def get_diff(self, inst, repo, number):
        return DIFF

    def get_file(self, inst, repo, path, ref):
        return self.cfg if path == ".ai-agents.yml" else SOURCE

    def create_check_run(self, *a, **k):
        self.checks.append(k)

    def post_comment(self, inst, repo, number, body):
        self.comments.append(body)


def run_agent(store, repo_root, cfg, replies):
    gh = GH(cfg)
    llm = LLMGateway(ScriptedBackend(replies=replies), store, sleep=lambda s: None)
    gen = TestGenerator(llm, PromptRegistry(repo_root / "prompts"), LocalTestSandbox())
    agent = SecurityAgent(
        gh, llm, PromptRegistry(repo_root / "prompts"), store, [SecretsScanner()], testgen=gen
    )
    job, _ = store.enqueue(
        AgentKind.SECURITY,
        "o/r#1",
        "sha1",
        "v1",
        {"repo": "o/r", "number": 1, "head_sha": "sha1", "installation_id": 1},
    )
    run_job(store, job.id, {AgentKind.SECURITY: agent.handle})
    return gh, store.result(job.id)


def test_verified_tests_posted_only_when_repo_opts_in(store, repo_root):
    gh, res = run_agent(store, repo_root, "tests:\n  enabled: true\n", [{"code": GOOD_TESTS}])
    assert res["tests_verified"] == 2
    body = gh.comments[0]
    assert "test_add_happy" in body and "test_wrong_expectation" not in body
    assert "isolated sandbox" in body and any(
        e["event"] == "security.tests_posted" for e in store.audit_events()
    )


def test_testgen_is_off_by_default(store, repo_root):
    gh, res = run_agent(store, repo_root, None, [])  # no replies: the LLM must not be called
    assert res["tests_verified"] == 0 and gh.comments == []


# ---- Azure sessions adapter contract -----------------------------------------------------------
class Tok:
    def get_token(self, *scopes):
        return type("T", (), {"token": "tok"})()


def sandbox_with(handler):
    return AzureSessionsSandbox("https://pool.example/sessions", "sid", Tok(), httpx.MockTransport(handler))


def test_azure_sessions_request_shape_and_result_parsing():
    seen = {}

    def handler(req):
        seen.update(
            path=req.url.path,
            params=dict(req.url.params),
            auth=req.headers["authorization"],
            body=req.content.decode(),
        )
        result = '{"code": 0, "stdout": "PASSED t.py::test_a", "stderr": "", "timeout": false}'
        return httpx.Response(200, json={"properties": {"stdout": f"noise\n@@RESULT@@{result}\n"}})

    r = sandbox_with(handler).run_pytest({"a.py": "x=1"}, "t.py", 30)
    assert r.exit_code == 0 and parse_pytest_outcomes(r.stdout) == {"test_a": "PASSED"}
    assert seen["path"].endswith("/code/execute") and seen["params"]["identifier"] == "sid"
    assert seen["auth"] == "Bearer tok" and "a.py" in seen["body"] and "t.py" in seen["body"]


def test_azure_sessions_failures_become_sandbox_unavailable():
    with pytest.raises(SandboxUnavailable):
        sandbox_with(lambda r: httpx.Response(500)).run_pytest({}, "t.py")
    with pytest.raises(SandboxUnavailable):
        sandbox_with(lambda r: httpx.Response(200, json={"properties": {"stdout": "nothing"}})).run_pytest(
            {}, "t.py"
        )
    missing = '{"code": 1, "stdout": "", "stderr": "No module named pytest", "timeout": false}'
    with pytest.raises(SandboxUnavailable, match="pytest"):
        sandbox_with(
            lambda r: httpx.Response(200, json={"properties": {"stdout": f"@@RESULT@@{missing}"}})
        ).run_pytest({}, "t.py")
