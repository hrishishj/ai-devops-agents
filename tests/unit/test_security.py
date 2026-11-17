from __future__ import annotations

from devopsagents.agents.security.workflow import SecurityAgent
from devopsagents.core.jobs.runner import Outcome, run_job
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMGateway
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.client import PullRequestInfo
from devopsagents.integrations.scanners.base import SecretsScanner, parse_semgrep

AWS = "AKIAIOSFODNN7EXAMPLE"
APP = f'''import os
KEY = "{AWS}"
password = "hunter2hunter2"
old_secret = "OLDsecretVALUE999"
'''
DIFF = """diff --git a/app/cfg.py b/app/cfg.py
--- a/app/cfg.py
+++ b/app/cfg.py
@@ -1,2 +1,4 @@
 import os
+KEY = "AKIAIOSFODNN7EXAMPLE"
+password = "hunter2hunter2"
 old_secret = "OLDsecretVALUE999"
"""


class FakeGH:
    def __init__(self, cfg=None):
        self.cfg, self.checks = cfg, []

    def get_pr(self, inst, repo, number):
        return PullRequestInfo(repo, number, "t", "b", "sha1", "base")

    def get_diff(self, inst, repo, number):
        return DIFF

    def get_file(self, inst, repo, path, ref):
        return self.cfg if path == ".ai-agents.yml" else APP

    def create_check_run(self, inst, repo, head_sha, name, conclusion, title, summary, annotations):
        self.checks.append(dict(conclusion=conclusion, title=title, summary=summary, ann=annotations))


def setup(store, repo_root, gh, replies):
    llm = LLMGateway(ScriptedBackend(replies=replies), store, sleep=lambda s: None)
    agent = SecurityAgent(gh, llm, PromptRegistry(repo_root / "prompts"), store, [SecretsScanner()])
    job, _ = store.enqueue(
        AgentKind.SECURITY,
        "o/r#1",
        "sha1",
        "v1",
        {"repo": "o/r", "number": 1, "head_sha": "sha1", "installation_id": 1},
    )
    return agent, job


def test_secrets_scanner_finds_and_masks():
    f = SecretsScanner().scan({"a.py": APP})
    rules = {x.rule for x in f}
    assert {"aws-access-key", "hardcoded-credential"} <= rules
    assert all(AWS not in x.code for x in f)


def test_only_changed_lines_reported_and_blocking_on_critical(store, repo_root):
    gh = FakeGH()
    agent, job = setup(
        store,
        repo_root,
        gh,
        [
            {
                "items": [
                    {"id": 0, "verdict": "true_positive", "explanation": "live key", "fix": "rotate"},
                    {"id": 1, "verdict": "true_positive", "explanation": "x", "fix": "env var"},
                ]
            }
        ],
    )
    assert run_job(store, job.id, {AgentKind.SECURITY: agent.handle}) == Outcome.SUCCEEDED
    check = gh.checks[0]
    assert check["conclusion"] == "failure"  # critical AWS key, fail_on default = critical
    lines = {a["start_line"] for a in check["ann"]}
    assert lines == {2, 3}  # the pre-existing secret on line 4 is NOT reported
    assert AWS not in check["summary"] and all(AWS not in a["message"] for a in check["ann"])


def test_llm_cannot_dismiss_high_precision_findings(store, repo_root):
    gh = FakeGH()
    agent, job = setup(
        store,
        repo_root,
        gh,
        [{"items": [{"id": 0, "verdict": "false_positive"}, {"id": 1, "verdict": "false_positive"}]}],
    )
    run_job(store, job.id, {AgentKind.SECURITY: agent.handle})
    rules = [a["title"] for a in gh.checks[0]["ann"]]
    assert len(rules) == 1 and rules[0].startswith("aws-access-key")  # credential FP dismissed


def test_triage_matches_by_id_not_position(store, repo_root):
    gh = FakeGH()
    # Items returned out of order and with one missing: explanations must still line up by id.
    agent, job = setup(
        store,
        repo_root,
        gh,
        [{"items": [{"id": 1, "verdict": "uncertain", "explanation": "about-cred", "fix": ""}]}],
    )
    run_job(store, job.id, {AgentKind.SECURITY: agent.handle})
    by_title = {a["title"].split()[0]: a["message"] for a in gh.checks[0]["ann"]}
    assert "about-cred" in by_title["hardcoded-credential"]
    assert "about-cred" not in by_title["aws-access-key"]


def test_fail_on_is_configurable_and_disable_works(store, repo_root):
    gh = FakeGH(cfg="security:\n  fail_on: []\n")
    agent, job = setup(store, repo_root, gh, [{"items": []}])
    run_job(store, job.id, {AgentKind.SECURITY: agent.handle})
    assert gh.checks[0]["conclusion"] == "success"

    gh2 = FakeGH(cfg="security:\n  enabled: false\n")
    agent2, job2 = setup(store, repo_root, gh2, [])
    store.cancel(job.id)
    job2, _ = store.enqueue(
        AgentKind.SECURITY,
        "o/r#2",
        "sha1",
        "v1",
        {"repo": "o/r", "number": 2, "head_sha": "sha1", "installation_id": 1},
    )
    run_job(store, job2.id, {AgentKind.SECURITY: agent2.handle})
    assert gh2.checks == []


def test_parse_semgrep_output():
    out = (
        '{"results":[{"check_id":"python.sqli","path":"./a.py","start":{"line":7},'
        '"extra":{"severity":"ERROR","message":"sqli","lines":"x","metadata":{"cwe":["CWE-89"]}}}]}'
    )
    f = parse_semgrep(out)[0]
    assert (f.path, f.line, f.severity.value, f.cwe) == ("a.py", 7, "high", "CWE-89")
    assert parse_semgrep("garbage") == []
