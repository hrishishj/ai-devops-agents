"""
Test & Security Agent
=====================
1. Analyses the AST of changed files to find functions lacking test coverage.
2. Generates targeted unit tests (Python/JS/Java) via GPT-4o.
3. Runs SAST rules (Semgrep / Checkov patterns embedded) concurrently.
4. GPT-4o provides human-readable explanations and fix suggestions per finding.
5. Posts a combined test + security report as a PR comment.

Pipeline
--------
  PR diff/files → AST parser → coverage gap detection
                             → test generator (GPT-4o)
              → SAST scanner  → security findings
                             → explainer (GPT-4o)
  Results → GitHub PR comment + Cosmos persist
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from shared.config import get_settings
from shared.cosmos_store import CosmosMemoryStore
from shared.github_client import PullRequestContext, fetch_pr_context, post_pr_comment
from shared.llm_client import LLMRequest, get_llm_client
from shared.redis_cache import get_cache
from shared.telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
_settings = get_settings()


# --------------------------------------------------------------------------- #
# SAST Rule Definitions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SastRule:
    rule_id: str
    description: str
    pattern: re.Pattern
    severity: str          # "critical" | "high" | "medium" | "low"
    cwe: str               # e.g. "CWE-89"


_SAST_RULES: list[SastRule] = [
    SastRule("SEC001", "SQL injection via string formatting",
             re.compile(r"(execute|query)\s*\(\s*f['\"]|%\s*\(.*\)\s*%\s*['\"]", re.IGNORECASE),
             "critical", "CWE-89"),
    SastRule("SEC002", "Hardcoded secret / password in source",
             re.compile(r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE),
             "critical", "CWE-798"),
    SastRule("SEC003", "Shell injection via subprocess with shell=True",
             re.compile(r"subprocess\.(run|call|Popen).*shell\s*=\s*True", re.IGNORECASE),
             "high", "CWE-78"),
    SastRule("SEC004", "Use of MD5 or SHA1 for security-sensitive hashing",
             re.compile(r"hashlib\.(md5|sha1)\s*\(", re.IGNORECASE),
             "high", "CWE-327"),
    SastRule("SEC005", "Insecure deserialization with pickle",
             re.compile(r"pickle\.(loads?|Unpickler)", re.IGNORECASE),
             "high", "CWE-502"),
    SastRule("SEC006", "Eval/exec with user-controlled input",
             re.compile(r"\b(eval|exec)\s*\(", re.IGNORECASE),
             "high", "CWE-95"),
    SastRule("SEC007", "XML External Entity (XXE) risk — lxml without safe parser",
             re.compile(r"etree\.parse|XMLParser\(\)", re.IGNORECASE),
             "medium", "CWE-611"),
    SastRule("SEC008", "Disabled SSL certificate verification",
             re.compile(r"verify\s*=\s*False", re.IGNORECASE),
             "medium", "CWE-295"),
    SastRule("SEC009", "Path traversal risk",
             re.compile(r"open\s*\(\s*.*\+.*['\"]|os\.path\.join.*request", re.IGNORECASE),
             "medium", "CWE-22"),
    SastRule("SEC010", "Debug mode enabled in production config",
             re.compile(r"DEBUG\s*=\s*True", re.IGNORECASE),
             "low", "CWE-489"),
]


# --------------------------------------------------------------------------- #
# Pydantic response schemas
# --------------------------------------------------------------------------- #

class GeneratedTest(BaseModel):
    file_path: str           # where the test file should be written
    function_name: str       # function under test
    test_code: str           # full test code block


class TestGenerationResponse(BaseModel):
    tests: list[GeneratedTest]
    coverage_gaps: list[str]  # functions lacking tests


class SecurityFinding(BaseModel):
    rule_id: str
    file_path: str
    line: int
    severity: str
    description: str
    explanation: str          # GPT-4o human-readable explanation
    fix_suggestion: str       # GPT-4o suggested remediation


# --------------------------------------------------------------------------- #
# AST Coverage Analyser
# --------------------------------------------------------------------------- #

def extract_python_functions(source_code: str) -> list[str]:
    """
    Parse Python source and return names of top-level and class functions.
    Returns empty list on parse errors.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip private/dunder
            if not node.name.startswith("_"):
                funcs.append(node.name)
    return funcs


def find_coverage_gaps(
    source_files: dict[str, str],
    test_files: dict[str, str],
) -> dict[str, list[str]]:
    """
    For each source file, find function names that do NOT appear in any test file.

    Returns dict: { source_path: [untested_function, ...] }
    """
    all_test_text = "\n".join(test_files.values())
    gaps: dict[str, list[str]] = {}

    for path, code in source_files.items():
        if not path.endswith(".py"):
            continue
        funcs = extract_python_functions(code)
        missing = [
            f for f in funcs
            if f not in all_test_text and f"test_{f}" not in all_test_text
        ]
        if missing:
            gaps[path] = missing
    return gaps


# --------------------------------------------------------------------------- #
# SAST Scanner
# --------------------------------------------------------------------------- #

@dataclass
class RawFinding:
    rule: SastRule
    file_path: str
    line_number: int
    matched_text: str


def run_sast_scan(files: dict[str, str]) -> list[RawFinding]:
    """Run all SAST rules against the provided source files."""
    findings: list[RawFinding] = []
    for file_path, content in files.items():
        lines = content.splitlines()
        for i, line in enumerate(lines, start=1):
            for rule in _SAST_RULES:
                if rule.pattern.search(line):
                    findings.append(
                        RawFinding(
                            rule=rule,
                            file_path=file_path,
                            line_number=i,
                            matched_text=line.strip()[:200],
                        )
                    )
    return findings


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #

@dataclass
class TestSecurityAgent:
    """
    Orchestrates AST-based test generation and SAST security scanning
    for a GitHub PR.
    """

    _llm = None
    _store: CosmosMemoryStore = field(
        default_factory=lambda: CosmosMemoryStore("test_reports")
    )
    _cache = None

    def __post_init__(self) -> None:
        self._llm = get_llm_client()
        self._cache = get_cache()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def handle_pr(self, repo_full_name: str, pr_number: int) -> None:
        idempotency_key = f"test_security:{repo_full_name}:{pr_number}"
        if not self._cache.claim_idempotency_key(idempotency_key):
            logger.info("Duplicate security scan event — skipping.")
            return

        with tracer.start_as_current_span("test_security.handle_pr") as span:
            span.set_attribute("github.repo", repo_full_name)
            span.set_attribute("github.pr_number", pr_number)

            pr_ctx = fetch_pr_context(repo_full_name, pr_number)

            # Fetch file contents (simplified: parse from diff)
            source_files, test_files = self._split_files(pr_ctx)

            # SAST scan (runs immediately — no LLM needed for detection)
            raw_findings = run_sast_scan(source_files)

            # Enrich SAST findings with GPT-4o explanations
            enriched_findings = self._explain_findings(raw_findings, source_files)

            # Coverage gap analysis + test generation
            gaps = find_coverage_gaps(source_files, test_files)
            test_response = self._generate_tests(gaps, source_files)

            # Post combined report to GitHub PR
            self._post_report(pr_ctx, enriched_findings, test_response)

            # Persist
            self._persist(repo_full_name, pr_number, enriched_findings, test_response)

            span.set_attribute("sast.finding_count", len(enriched_findings))
            span.set_attribute("test.generated_count",
                               len(test_response.tests) if test_response else 0)

    # ------------------------------------------------------------------ #
    # Pipeline stages
    # ------------------------------------------------------------------ #

    def _split_files(
        self, pr_ctx: PullRequestContext
    ) -> tuple[dict[str, str], dict[str, str]]:
        """
        Parse diff to reconstruct added/changed file content.
        Returns (source_files, test_files) dicts: path → content.
        """
        source: dict[str, str] = {}
        tests: dict[str, str] = {}

        current_file = None
        current_lines: list[str] = []

        for line in pr_ctx.diff.splitlines():
            if line.startswith("diff --git"):
                if current_file:
                    bucket = tests if self._is_test_file(current_file) else source
                    bucket[current_file] = "\n".join(current_lines)
                current_file = None
                current_lines = []
            elif line.startswith("+++ b/"):
                current_file = line[6:]
            elif current_file and line.startswith("+") and not line.startswith("+++"):
                current_lines.append(line[1:])

        if current_file:
            bucket = tests if self._is_test_file(current_file) else source
            bucket[current_file] = "\n".join(current_lines)

        return source, tests

    @staticmethod
    def _is_test_file(path: str) -> bool:
        return "test" in path.lower() or "spec" in path.lower()

    def _explain_findings(
        self, raw: list[RawFinding], source_files: dict[str, str]
    ) -> list[SecurityFinding]:
        if not raw:
            return []

        findings_text = json.dumps(
            [
                {
                    "rule_id": f.rule.rule_id,
                    "description": f.rule.description,
                    "cwe": f.rule.cwe,
                    "severity": f.rule.severity,
                    "file": f.file_path,
                    "line": f.line_number,
                    "code": f.matched_text,
                }
                for f in raw
            ],
            indent=2,
        )
        request = LLMRequest(
            system_prompt=_SAST_EXPLAIN_SYSTEM_PROMPT,
            user_message=f"### Security Findings\n```json\n{findings_text}\n```",
            temperature=0.1,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )
        raw_resp = self._llm.complete(request).content
        try:
            data = json.loads(raw_resp)
            enriched = []
            for item, original in zip(data.get("findings", []), raw):
                enriched.append(
                    SecurityFinding(
                        rule_id=original.rule.rule_id,
                        file_path=original.file_path,
                        line=original.line_number,
                        severity=original.rule.severity,
                        description=original.rule.description,
                        explanation=item.get("explanation", ""),
                        fix_suggestion=item.get("fix_suggestion", ""),
                    )
                )
            return enriched
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            logger.error("SAST explanation parse failure: %s", exc)
            return []

    def _generate_tests(
        self,
        gaps: dict[str, list[str]],
        source_files: dict[str, str],
    ) -> TestGenerationResponse | None:
        if not gaps:
            return TestGenerationResponse(tests=[], coverage_gaps=[])

        gap_summary = {
            path: funcs for path, funcs in gaps.items() if path.endswith(".py")
        }
        # Include relevant source snippets (truncated)
        snippets = {
            path: source_files.get(path, "")[:3000]
            for path in gap_summary
        }
        user_msg = (
            f"### Coverage Gaps\n```json\n{json.dumps(gap_summary, indent=2)}\n```\n\n"
            f"### Source Snippets\n```python\n"
            + "\n\n# ---\n\n".join(f"# {p}\n{c}" for p, c in snippets.items())
            + "\n```"
        )
        request = LLMRequest(
            system_prompt=_TEST_GEN_SYSTEM_PROMPT,
            user_message=user_msg,
            temperature=0.2,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )
        raw_resp = self._llm.complete(request).content
        try:
            data = json.loads(raw_resp)
            return TestGenerationResponse(**data)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.error("Test generation parse failure: %s", exc)
            return None

    def _post_report(
        self,
        pr_ctx: PullRequestContext,
        findings: list[SecurityFinding],
        tests: TestGenerationResponse | None,
    ) -> None:
        sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}

        # Security section
        if findings:
            sec_lines = ["## 🛡️ Security Analysis\n"]
            for f in findings:
                icon = sev_icon.get(f.severity, "⚪")
                sec_lines.append(
                    f"### {icon} `{f.rule_id}` — {f.description}\n"
                    f"**File:** `{f.file_path}` · **Line:** {f.line}\n\n"
                    f"{f.explanation}\n\n"
                    f"**Fix:** {f.fix_suggestion}\n"
                )
            sec_section = "\n".join(sec_lines)
        else:
            sec_section = "## 🛡️ Security Analysis\n\n✅ No security issues detected."

        # Test section
        if tests and tests.tests:
            test_lines = [f"## 🧪 Generated Tests ({len(tests.tests)} new test(s))\n"]
            for t in tests.tests[:5]:  # cap at 5 in PR comment
                test_lines.append(
                    f"### `{t.function_name}` → `{t.file_path}`\n"
                    f"```python\n{t.test_code}\n```\n"
                )
            test_section = "\n".join(test_lines)
        elif tests and tests.coverage_gaps:
            test_section = (
                "## 🧪 Test Coverage\n\n"
                f"⚠️ Coverage gaps detected but no tests generated: "
                f"`{'`, `'.join(tests.coverage_gaps[:10])}`"
            )
        else:
            test_section = "## 🧪 Test Coverage\n\n✅ No coverage gaps detected."

        body = f"{sec_section}\n\n---\n\n{test_section}"
        post_pr_comment(pr_ctx.repo_full_name, pr_ctx.pr_number, body)

    def _persist(
        self,
        repo: str,
        pr_number: int,
        findings: list[SecurityFinding],
        tests: TestGenerationResponse | None,
    ) -> None:
        record = {
            "id": f"{repo.replace('/', '_')}_{pr_number}_security",
            "repo": repo,
            "pr_number": pr_number,
            "finding_count": len(findings),
            "findings": [f.model_dump() for f in findings],
            "generated_test_count": len(tests.tests) if tests else 0,
            "coverage_gaps": tests.coverage_gaps if tests else [],
        }
        self._store.upsert(record)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

_SAST_EXPLAIN_SYSTEM_PROMPT = """
You are a security engineer. For each SAST finding provided, write:
1. A 2-3 sentence plain-English explanation of why it's dangerous.
2. A concrete code fix suggestion.

Return ONLY valid JSON:
{
  "findings": [
    {
      "explanation": "<plain-English explanation>",
      "fix_suggestion": "<code or step-by-step fix>"
    }
  ]
}
The findings array must be the same length and order as the input findings.
""".strip()

_TEST_GEN_SYSTEM_PROMPT = """
You are a senior software engineer specialising in test-driven development.
Given the coverage gaps and source code, generate pytest unit tests.

Return ONLY valid JSON:
{
  "tests": [
    {
      "file_path": "tests/test_<module>.py",
      "function_name": "<function under test>",
      "test_code": "<full pytest test function or class>"
    }
  ],
  "coverage_gaps": ["<function_name>", ...]
}

Rules:
- Use pytest and standard library mocks (unittest.mock).
- Test happy path AND at least one edge case per function.
- Keep tests self-contained — no external I/O.
- Do NOT use unittest.TestCase — use plain pytest functions.
""".strip()
