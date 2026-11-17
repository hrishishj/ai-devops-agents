"""Verified test generation: propose pytest tests for changed functions, run them in an isolated
sandbox, and keep only tests that actually pass. Nothing generated is ever executed in-process."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from devopsagents.core.llm.gateway import LLMGateway, ModelTier
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.integrations.sandbox.base import (
    Sandbox,
    SandboxUnavailable,
    parse_pytest_outcomes,
)

log = logging.getLogger(__name__)

_BANNED_IMPORTS = {
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "urllib",
    "http",
    "ftplib",
    "smtplib",
    "shutil",
    "ctypes",
    "multiprocessing",
    "os",
    "sys",
    "importlib",
    "pty",
    "signal",
    "pickle",
    "marshal",
}
_BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint"}

_CONFTEST = (
    "import sys, pathlib\n"
    "root = pathlib.Path(__file__).parent\n"
    "sys.path[:0] = [str(root), str(root / 'src')]\n"
)
TEST_FILE = "generated/test_generated.py"


class GeneratedTestFile(BaseModel):
    code: str


@dataclass(frozen=True)
class Target:
    path: str
    functions: list[str]
    source: str


@dataclass
class VerifiedTests:
    path: str  # source file the tests cover
    code: str
    test_names: list[str]
    dropped: int = 0  # generated tests that failed and were removed
    skipped_reason: str = ""
    coverage: dict[str, float] = field(default_factory=dict)  # target function -> % of its body lines run
    coverage_measured: bool = False


@dataclass
class TestgenReport:
    __test__ = False  # not a pytest test class

    verified: list[VerifiedTests] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def function_coverage(
    source: str, functions: list[str], info: dict[str, list[int]] | None
) -> dict[str, float] | None:
    """Percent of each target function's body statements that the tests executed.

    `info` is the sandbox's {"executed": [...], "missing": [...]} for the source file. The `def` line itself
    is ignored: it runs at import time even when the function is never called.
    """
    if info is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    executed, missing = set(info.get("executed", [])), set(info.get("missing", []))
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name not in found:
            found[node.name] = node
    out: dict[str, float] = {}
    for name in functions:
        fn = found.get(name)
        if fn is None:
            continue
        body = set(range(fn.lineno + 1, (fn.end_lineno or fn.lineno) + 1))
        statements = (executed | missing) & body
        out[name] = 100.0 * len(executed & body) / len(statements) if statements else 100.0
    return out


def import_path(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    mod = mod.removeprefix("src.").removesuffix(".__init__")
    return mod


def find_targets(
    sources: dict[str, str],
    added: dict[str, set[int]],
    existing_tests_text: str,
    max_functions: int,
    max_files: int,
) -> list[Target]:
    """Public functions whose body overlaps lines added by the PR and that no test mentions."""
    targets: list[Target] = []
    budget = max_functions
    for path in sorted(sources):
        if budget <= 0 or len(targets) >= max_files:
            break
        if not path.endswith(".py") or "test" in path.lower():
            continue
        try:
            tree = ast.parse(sources[path])
        except SyntaxError:
            continue
        touched = added.get(path, set())
        names: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name.startswith("_") or node.name in existing_tests_text:
                continue
            span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if span & touched and node.name not in names:
                names.append(node.name)
        names = names[:budget]
        if names:
            targets.append(Target(path, names, sources[path]))
            budget -= len(names)
    return targets


def guard(code: str) -> str | None:
    """Static defence-in-depth before sending generated code to the sandbox. Returns a reason to reject."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _BANNED_IMPORTS:
                    return f"banned import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _BANNED_IMPORTS:
                return f"banned import {node.module}"
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BANNED_CALLS:
                return f"banned call {fn.id}()"
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return "test classes are not supported; use plain functions"
    return None


def split_tests(code: str) -> tuple[str, dict[str, str]] | None:
    """(shared header source, {test_name: function source}); None if unparsable."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    header: list[str] = []
    tests: dict[str, str] = {}
    for node in tree.body:
        seg = ast.get_source_segment(code, node) or ""
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            # include decorators (e.g. parametrize), which get_source_segment omits
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            seg = "\n".join(code.splitlines()[start - 1 : node.end_lineno])
            tests[node.name] = seg
        else:
            header.append(seg)
    return "\n".join(header), tests


def _assemble(header: str, tests: dict[str, str], names: list[str]) -> str:
    return header.rstrip() + "\n\n\n" + "\n\n\n".join(tests[n] for n in names) + "\n"


class TestGenerator:
    __test__ = False  # not a pytest class

    def __init__(
        self, gateway: LLMGateway, prompts: PromptRegistry, sandbox: Sandbox, *, timeout: int = 60
    ) -> None:
        self._llm, self._prompts, self._sandbox, self._timeout = gateway, prompts, sandbox, timeout

    def run(
        self, targets: list[Target], scope: str, timeout: int | None = None, min_coverage: int = 0
    ) -> TestgenReport:
        report = TestgenReport()
        prompt = self._prompts.get("security", "testgen")
        for t in targets:
            user = (
                f"<import_path>{import_path(t.path)}</import_path>\n"
                f"<targets>{', '.join(t.functions)}</targets>\n"
                f'<source path="{t.path}">\n{t.source[:12000]}\n</source>'
            )
            try:
                out = self._llm.structured(
                    tier=ModelTier.LARGE,
                    system=prompt.text,
                    user=user,
                    response_model=GeneratedTestFile,
                    scope=scope,
                    prompt_id=prompt.id,
                    max_tokens=2500,
                )
                result = self._verify(t, out.code, timeout or self._timeout, min_coverage)
            except SandboxUnavailable as exc:
                report.notes.append(f"sandbox unavailable ({exc}); skipped test generation")
                return report
            if result.test_names:
                report.verified.append(result)
            else:
                report.notes.append(f"{t.path}: {result.skipped_reason}")
        return report

    def _verify(self, t: Target, code: str, timeout: int, min_coverage: int = 0) -> VerifiedTests:
        def none(reason: str) -> VerifiedTests:
            return VerifiedTests(t.path, "", [], skipped_reason=reason)

        if (reason := guard(code)) is not None:
            return none(f"rejected generated tests ({reason})")
        parts = split_tests(code)
        if parts is None or not parts[1]:
            return none("no usable test functions generated")
        header, tests = parts

        def execute(names: list[str]) -> tuple[dict[str, str], dict[str, list[int]] | None]:
            files = {t.path: t.source, "conftest.py": _CONFTEST, TEST_FILE: _assemble(header, tests, names)}
            res = self._sandbox.run_pytest(files, TEST_FILE, timeout, cover=[t.path])
            if res.timed_out:
                return {}, None
            return parse_pytest_outcomes(res.stdout), (res.coverage or {}).get(t.path)

        first, cov = execute(list(tests))
        passed = [n for n in tests if first.get(n) == "PASSED"]
        if not passed:
            return none("none of the generated tests passed in the sandbox")
        if len(passed) < len(tests):
            # confirm the surviving subset passes on its own (shared state/order effects)
            second, cov = execute(passed)
            passed = [n for n in passed if second.get(n) == "PASSED"]
            if not passed:
                return none("tests were not stable when re-run")

        per_function = function_coverage(t.source, t.functions, cov)
        if per_function is not None:
            if per_function and max(per_function.values()) == 0:
                return none("the tests pass but never execute the target functions")
            mean = sum(per_function.values()) / len(per_function) if per_function else 100.0
            if mean < min_coverage:
                return none(f"tests cover {mean:.0f}% of the target functions (minimum {min_coverage}%)")
        return VerifiedTests(
            t.path,
            _assemble(header, tests, passed),
            passed,
            dropped=len(tests) - len(passed),
            coverage=per_function or {},
            coverage_measured=per_function is not None,
        )
