"""
Unit tests for agents/test_security/agent.py
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.test_security.agent import (
    TestSecurityAgent,
    GeneratedTest,
    TestGenerationResponse,
    SecurityFinding,
    SastRule,
    RawFinding,
    extract_python_functions,
    find_coverage_gaps,
    run_sast_scan,
    _SAST_RULES,
)


# --------------------------------------------------------------------------- #
# extract_python_functions
# --------------------------------------------------------------------------- #

class TestExtractPythonFunctions:
    def test_extracts_top_level_functions(self):
        code = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        fns = extract_python_functions(code)
        assert "foo" in fns
        assert "bar" in fns

    def test_ignores_private_functions(self):
        code = "def _private():\n    pass\ndef public():\n    pass\n"
        fns = extract_python_functions(code)
        assert "_private" not in fns
        assert "public" in fns

    def test_extracts_async_functions(self):
        code = "async def fetch_data():\n    pass\n"
        fns = extract_python_functions(code)
        assert "fetch_data" in fns

    def test_extracts_class_methods(self):
        code = "class MyClass:\n    def process(self):\n        pass\n"
        fns = extract_python_functions(code)
        assert "process" in fns

    def test_invalid_python_returns_empty(self):
        fns = extract_python_functions("def broken(:\n    pass")
        assert fns == []

    def test_empty_file_returns_empty(self):
        fns = extract_python_functions("")
        assert fns == []

    def test_ignores_dunder_methods(self):
        code = "class A:\n    def __init__(self):\n        pass\n    def run(self):\n        pass\n"
        fns = extract_python_functions(code)
        assert "__init__" not in fns
        assert "run" in fns


# --------------------------------------------------------------------------- #
# find_coverage_gaps
# --------------------------------------------------------------------------- #

class TestFindCoverageGaps:
    def test_detects_untested_function(self):
        source = {"app.py": "def compute():\n    pass\n"}
        tests = {"test_app.py": "def test_other():\n    pass\n"}
        gaps = find_coverage_gaps(source, tests)
        assert "compute" in gaps.get("app.py", [])

    def test_no_gap_when_test_exists(self):
        source = {"app.py": "def compute():\n    pass\n"}
        tests = {"test_app.py": "def test_compute():\n    pass\n"}
        gaps = find_coverage_gaps(source, tests)
        assert "compute" not in gaps.get("app.py", [])

    def test_detects_gap_by_function_name_presence(self):
        source = {"app.py": "def process():\n    pass\n"}
        tests = {"test_app.py": "# process is mocked elsewhere\n"}
        gaps = find_coverage_gaps(source, tests)
        # "process" appears in test file text — should not flag it
        assert "process" not in gaps.get("app.py", [])

    def test_skips_non_python_files(self):
        source = {"main.js": "function doSomething() {}"}
        tests = {}
        gaps = find_coverage_gaps(source, tests)
        assert "main.js" not in gaps

    def test_empty_source_no_gaps(self):
        gaps = find_coverage_gaps({}, {})
        assert gaps == {}

    def test_multiple_files(self):
        source = {
            "a.py": "def alpha():\n    pass\n",
            "b.py": "def beta():\n    pass\n",
        }
        tests = {"test_a.py": "def test_alpha():\n    pass\n"}
        gaps = find_coverage_gaps(source, tests)
        assert "alpha" not in gaps.get("a.py", [])
        assert "beta" in gaps.get("b.py", [])


# --------------------------------------------------------------------------- #
# run_sast_scan
# --------------------------------------------------------------------------- #

class TestRunSastScan:
    def test_detects_sql_injection(self):
        files = {"db.py": "conn.execute(f'SELECT * FROM users WHERE id={user_id}')"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC001" in rule_ids

    def test_detects_hardcoded_password(self):
        files = {"config.py": "password = 'supersecret123'"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC002" in rule_ids

    def test_detects_shell_injection(self):
        files = {"runner.py": "subprocess.run(cmd, shell=True)"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC003" in rule_ids

    def test_detects_md5(self):
        files = {"crypto.py": "digest = hashlib.md5(data).hexdigest()"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC004" in rule_ids

    def test_detects_pickle(self):
        files = {"cache.py": "obj = pickle.loads(data)"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC005" in rule_ids

    def test_detects_eval(self):
        files = {"parser.py": "result = eval(user_input)"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC006" in rule_ids

    def test_detects_ssl_verify_false(self):
        files = {"client.py": "requests.get(url, verify=False)"}
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC008" in rule_ids

    def test_clean_file_no_findings(self):
        files = {"clean.py": "def add(a, b):\n    return a + b\n"}
        findings = run_sast_scan(files)
        assert findings == []

    def test_finding_includes_correct_line_number(self):
        files = {"db.py": "x = 1\ny = 2\npassword = 'secret123'\n"}
        findings = run_sast_scan(files)
        pw_findings = [f for f in findings if f.rule.rule_id == "SEC002"]
        assert pw_findings[0].line_number == 3

    def test_multiple_files_scanned(self):
        files = {
            "a.py": "password = 'abc123'",
            "b.py": "eval(user_input)",
        }
        findings = run_sast_scan(files)
        rule_ids = [f.rule.rule_id for f in findings]
        assert "SEC002" in rule_ids
        assert "SEC006" in rule_ids

    def test_finding_includes_file_path(self):
        files = {"mymodule/db.py": "password = 'hunter2'"}
        findings = run_sast_scan(files)
        assert findings[0].file_path == "mymodule/db.py"


# --------------------------------------------------------------------------- #
# TestSecurityAgent fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_llm():
    return MagicMock()


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.upsert.side_effect = lambda item: item
    return store


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.claim_idempotency_key.return_value = True
    return cache


@pytest.fixture
def agent(mock_llm, mock_store, mock_cache):
    with patch("agents.test_security.agent.get_llm_client", return_value=mock_llm), \
         patch("agents.test_security.agent.get_cache", return_value=mock_cache), \
         patch("agents.test_security.agent.CosmosMemoryStore", return_value=mock_store):
        a = TestSecurityAgent()
        a._llm = mock_llm
        a._store = mock_store
        a._cache = mock_cache
        return a


# --------------------------------------------------------------------------- #
# _split_files
# --------------------------------------------------------------------------- #

class TestSplitFiles:
    def test_separates_source_and_test_files(self, agent):
        pr_ctx = MagicMock()
        pr_ctx.diff = (
            "diff --git a/app.py b/app.py\n"
            "+++ b/app.py\n"
            "+def hello():\n"
            "+    pass\n"
            "diff --git a/tests/test_app.py b/tests/test_app.py\n"
            "+++ b/tests/test_app.py\n"
            "+def test_hello():\n"
            "+    pass\n"
        )
        source, tests = agent._split_files(pr_ctx)
        assert "app.py" in source
        assert "tests/test_app.py" in tests
        assert "hello" in source["app.py"]

    def test_empty_diff_returns_empty_dicts(self, agent):
        pr_ctx = MagicMock()
        pr_ctx.diff = ""
        source, tests = agent._split_files(pr_ctx)
        assert source == {}
        assert tests == {}


# --------------------------------------------------------------------------- #
# handle_pr — integration of steps
# --------------------------------------------------------------------------- #

class TestHandlePrIntegration:
    def test_duplicate_event_skipped(self, agent, mock_cache):
        mock_cache.claim_idempotency_key.return_value = False
        with patch("agents.test_security.agent.fetch_pr_context") as mock_fetch:
            agent.handle_pr("org/repo", 1)
            mock_fetch.assert_not_called()

    def test_no_findings_posts_clean_report(self, agent, mock_llm):
        pr_ctx = MagicMock()
        pr_ctx.diff = ""
        pr_ctx.repo_full_name = "org/repo"
        pr_ctx.pr_number = 1
        pr_ctx.changed_files = []

        mock_llm.complete.return_value = MagicMock(
            content=json.dumps({"findings": [], "tests": [], "coverage_gaps": []})
        )

        with patch("agents.test_security.agent.fetch_pr_context", return_value=pr_ctx), \
             patch("agents.test_security.agent.post_pr_comment") as mock_post:
            agent.handle_pr("org/repo", 1)
            mock_post.assert_called_once()
            body = mock_post.call_args[0][2]
            assert "No security issues detected" in body
