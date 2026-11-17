from __future__ import annotations

import json

import httpx
import pytest

from devopsagents.agents.security.testgen import (
    TestgenReport,
    VerifiedTests,
    find_targets,
    function_coverage,
)
from devopsagents.agents.security.workflow import _format_tests_comment
from devopsagents.core.config.repo_config import parse_repo_config
from devopsagents.integrations.sandbox.azure_sessions import AzureSessionsSandbox
from devopsagents.integrations.sandbox.base import SandboxResult, parse_runner_output
from tests.unit.test_docker_sandbox import FakeRun, sandbox
from tests.unit.test_testgen import GOOD_TESTS, SOURCE, LocalTestSandbox, generator

PATH = "src/pkg/calc.py"
ADD_ONLY = "from pkg.calc import add\n\n\ndef test_add_only():\n    assert add(2, 3) == 5\n"
VACUOUS = "from pkg.calc import add, divide\n\n\ndef test_nothing():\n    assert True\n"


def targets():
    return find_targets({PATH: SOURCE}, {PATH: {2, 6}}, "", 5, 3)  # add() and divide()


def run(store, repo_root, code, *, min_coverage=0, sandbox_=None):
    gen, sb = generator(store, repo_root, [{"code": code}], sandbox_)
    return gen.run(targets(), "o/r", None, min_coverage), sb


# ---- function_coverage (pure) --------------------------------------------------------------------------
def test_coverage_is_computed_over_body_statements_not_the_def_line():
    info = {"executed": [1, 2, 5, 6, 7], "missing": [8]}  # lines 1 and 5 are the `def` lines (run on import)
    assert function_coverage(SOURCE, ["add", "divide"], info) == {
        "add": 100.0,
        "divide": pytest.approx(200 / 3),
    }


def test_an_uncalled_function_is_zero_even_though_its_def_line_ran():
    info = {"executed": [1, 5], "missing": [2, 6, 7, 8]}
    assert function_coverage(SOURCE, ["add", "divide"], info) == {"add": 0.0, "divide": 0.0}


def test_coverage_edge_cases():
    assert function_coverage(SOURCE, ["add"], None) is None  # not measured
    assert function_coverage("def broken(:", ["x"], {"executed": [], "missing": []}) is None
    assert function_coverage(SOURCE, ["unknown"], {"executed": [], "missing": []}) == {}
    src = "async def go():\n    await x()\n    return 1\n\n\nclass K:\n    def m(self):\n        return 2\n"
    info = {"executed": [2, 8], "missing": [3]}
    assert function_coverage(src, ["go", "m"], info) == {"go": 50.0, "m": 100.0}  # async def and methods
    only_doc = 'def f():\n    """docs"""\n'
    assert function_coverage(only_doc, ["f"], {"executed": [], "missing": []}) == {
        "f": 100.0
    }  # no statements


# ---- the real runner measures coverage -----------------------------------------------------------------
def test_the_runner_reports_which_lines_of_the_target_ran(store, repo_root):
    report, sb = run(store, repo_root, ADD_ONLY)
    v = report.verified[0]
    assert v.coverage_measured and v.coverage == {"add": 100.0, "divide": 0.0}
    assert sb.covers[0] == [PATH]  # the generator asked for coverage of exactly the source file


def test_failed_tests_do_not_count_towards_coverage(store, repo_root):
    report, _ = run(store, repo_root, GOOD_TESTS)  # add, divide-by-zero pass; a wrong assertion is dropped
    v = report.verified[0]
    assert v.dropped == 1 and v.coverage["add"] == 100.0
    assert v.coverage["divide"] == pytest.approx(200 / 3)  # coverage comes from the confirmed passing subset


# ---- the gate: passing is not enough --------------------------------------------------------------------
def test_tests_that_pass_without_running_the_code_are_dropped(store, repo_root):
    report, _ = run(store, repo_root, VACUOUS)
    assert report.verified == []
    assert "never execute the target functions" in report.notes[0]


def test_min_coverage_is_enforced_on_the_average_of_the_target_functions(store, repo_root):
    low, _ = run(store, repo_root, ADD_ONLY, min_coverage=60)  # (100 + 0) / 2 = 50
    assert low.verified == [] and "cover 50% of the target functions (minimum 60%)" in low.notes[0]
    ok, _ = run(store, repo_root, ADD_ONLY, min_coverage=40)
    assert len(ok.verified) == 1


class NoCoverageSandbox(LocalTestSandbox):
    """Behaves like an image without the `coverage` package: tests run, coverage is simply absent."""

    def run_pytest(self, files, target, timeout_seconds=60, cover=None):
        res = super().run_pytest(files, target, timeout_seconds, cover)
        return SandboxResult(res.exit_code, res.stdout, res.stderr, res.timed_out, coverage=None)


def test_without_coverage_support_tests_are_kept_and_the_comment_says_so(store, repo_root):
    report, _ = run(store, repo_root, ADD_ONLY, min_coverage=90, sandbox_=NoCoverageSandbox())
    v = report.verified[0]  # a threshold cannot be enforced on something that was not measured
    assert not v.coverage_measured and v.coverage == {}
    assert "Coverage was not measured" in _format_tests_comment(report)


# ---- reporting -----------------------------------------------------------------------------------------
def test_the_pr_comment_shows_coverage_per_function():
    v = VerifiedTests(
        PATH,
        "def test_a(): pass",
        ["test_a"],
        dropped=1,
        coverage={"add": 100.0, "divide": 66.6},
        coverage_measured=True,
    )
    text = _format_tests_comment(TestgenReport(verified=[v]))
    assert "`add` 100%" in text and "`divide` 67%" in text and "1 generated test(s) failed" in text


# ---- plumbing ----------------------------------------------------------------------------------------------
def test_repo_config_validates_min_coverage():
    assert parse_repo_config("tests:\n  min_coverage: 70\n").tests.min_coverage == 70
    assert (
        parse_repo_config("tests:\n  min_coverage: 700\n").tests.min_coverage == 0
    )  # invalid -> safe default


def test_runner_output_carries_coverage_through_the_parser():
    out = json.dumps(
        {
            "code": 0,
            "stdout": "",
            "stderr": "",
            "timeout": False,
            "coverage": {PATH: {"executed": [1, 2], "missing": [3]}},
        }
    )
    res = parse_runner_output("noise\n@@RESULT@@" + out)
    assert res.coverage == {PATH: {"executed": [1, 2], "missing": [3]}}
    assert (
        parse_runner_output('@@RESULT@@{"code":0,"stdout":"","stderr":"","timeout":false}').coverage is None
    )


def test_docker_and_azure_adapters_forward_the_cover_list():
    run_ = FakeRun()
    sandbox(run_).run_pytest({"a.py": "x"}, "t.py", 30, cover=["a.py"])
    assert json.loads(run_.calls[0][1]["input"])["cover"] == ["a.py"]

    seen = {}

    class Tok:
        def get_token(self, *a):
            return type("T", (), {"token": "t"})()

    def handler(req):
        seen["code"] = json.loads(req.content)["properties"]["code"]
        result = json.dumps(
            {
                "code": 0,
                "stdout": "",
                "stderr": "",
                "timeout": False,
                "coverage": {"a.py": {"executed": [1], "missing": []}},
            }
        )
        return httpx.Response(200, json={"properties": {"stdout": "@@RESULT@@" + result}})

    az = AzureSessionsSandbox("https://pool", "sid", Tok(), httpx.MockTransport(handler))
    res = az.run_pytest({"a.py": "x"}, "t.py", 30, cover=["a.py"])
    assert '"cover": ["a.py"]' in seen["code"] and res.coverage == {"a.py": {"executed": [1], "missing": []}}
