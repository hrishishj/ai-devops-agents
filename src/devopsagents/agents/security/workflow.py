"""Security workflow: fetch changed files -> deterministic scanners -> LLM triage -> Check Run.

Detection is deterministic (secrets, Semgrep). The LLM only triages and explains; it can
dismiss low-precision findings but never high-precision ones (e.g. a real AWS key).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

from devopsagents.agents.review.chunking import is_ignored
from devopsagents.core.config.repo_config import parse_repo_config
from devopsagents.core.jobs.runner import NonRetryableError
from devopsagents.core.jobs.store import JobStore
from devopsagents.core.llm.gateway import LLMGateway, ModelTier
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.core.telemetry.tracing import span
from devopsagents.domain.diff import parse_unified_diff
from devopsagents.domain.findings import Severity
from devopsagents.domain.jobs import Job
from devopsagents.integrations.github.client import PullRequestInfo
from devopsagents.integrations.scanners.base import RawFinding, Scanner

log = logging.getLogger(__name__)

AGENT_VERSION = "security-1"
MAX_FILES = 100
MAX_FILE_BYTES = 200_000
_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


class GitHubPort(Protocol):
    def get_pr(self, inst: int, repo: str, number: int) -> PullRequestInfo: ...
    def get_diff(self, inst: int, repo: str, number: int) -> str: ...
    def get_file(self, inst: int, repo: str, path: str, ref: str) -> str | None: ...
    def create_check_run(
        self,
        inst: int,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        annotations: list[dict[str, Any]],
    ) -> None: ...


class TriageItem(BaseModel):
    id: int
    verdict: str = Field(description="true_positive | false_positive | uncertain")
    explanation: str = ""
    fix: str = ""


class TriageResult(BaseModel):
    items: list[TriageItem] = Field(default_factory=list)


class SecurityAgent:
    def __init__(
        self,
        github: GitHubPort,
        gateway: LLMGateway,
        prompts: PromptRegistry,
        store: JobStore,
        scanners: list[Scanner],
        *,
        kill_switch: bool = False,
    ) -> None:
        self._gh, self._llm, self._prompts, self._store = github, gateway, prompts, store
        self._scanners = {s.name: s for s in scanners}
        self._kill = kill_switch

    def handle(self, job: Job) -> dict[str, Any]:
        p = job.payload
        repo, number = str(p["repo"]), int(p["number"])  # type: ignore[call-overload]
        head_sha, inst = str(p["head_sha"]), int(p.get("installation_id") or 0)  # type: ignore[call-overload]
        if self._kill:
            return {"status": "skipped", "reason": "kill switch"}

        with span("security.handle", repo=repo, pr=number):
            cfg = parse_repo_config(self._gh.get_file(inst, repo, ".ai-agents.yml", head_sha))
            if not cfg.security.enabled:
                return {"status": "skipped", "reason": "security disabled"}
            pr = self._gh.get_pr(inst, repo, number)
            if pr.head_sha != head_sha:
                raise NonRetryableError("stale revision")

            diffs = parse_unified_diff(self._gh.get_diff(inst, repo, number))
            changed = {
                f.path: set(f.added_lines)
                for f in diffs
                if not f.is_deleted and not f.is_binary and not is_ignored(f.path, cfg.security.paths_ignore)
            }
            skipped_over = max(0, len(changed) - MAX_FILES)
            files: dict[str, str] = {}
            for path in list(changed)[:MAX_FILES]:
                text = self._gh.get_file(inst, repo, path, head_sha)
                if text is not None and len(text) <= MAX_FILE_BYTES:
                    files[path] = text

            raw: list[RawFinding] = []
            ran: list[str] = []
            for name in cfg.security.scanners:
                sc = self._scanners.get(name)
                if sc is None or (hasattr(sc, "available") and not sc.available):
                    continue
                ran.append(name)
                raw.extend(sc.scan(files))
            # Only report issues the PR touches.
            raw = [r for r in raw if r.line in changed.get(r.path, set())]

            triaged = self._triage(raw, repo)
            if not self._store.is_current(job.id):
                return {"status": "dropped", "reason": "superseded"}

            fail_on = set(cfg.security.fail_on)
            blocking = [f for f, _, _ in triaged if f.severity.value in fail_on]
            conclusion = "failure" if blocking else "success"
            self._gh.create_check_run(
                inst,
                repo,
                head_sha,
                "AI Security",
                conclusion,
                title=f"{len(triaged)} finding(s)" if triaged else "No security findings",
                summary=_summary(triaged, ran, len(files), skipped_over),
                annotations=[_annotation(f, why, fix) for f, why, fix in triaged],
            )
            self._store.audit(
                "security",
                "security.check_run",
                f"{repo}#{number}",
                {"findings": len(triaged), "conclusion": conclusion, "scanners": ran},
            )
            return {
                "status": "reported",
                "conclusion": conclusion,
                "findings": len(triaged),
                "scanners": ran,
                "files_scanned": len(files),
                "agent_version": AGENT_VERSION,
            }

    def _triage(self, raw: list[RawFinding], repo: str) -> list[tuple[RawFinding, str, str]]:
        if not raw:
            return []
        prompt = self._prompts.get("security", "triage")
        listing = "\n".join(
            f"[{i}] rule={r.rule} sev={r.severity.value} file={r.path}:{r.line}\n"
            f"    message: {r.message}\n    code: {r.code}"
            for i, r in enumerate(raw)
        )
        out = self._llm.structured(
            tier=ModelTier.SMALL,
            system=prompt.text,
            user=f"<findings>\n{listing}\n</findings>",
            response_model=TriageResult,
            scope=repo,
            prompt_id=prompt.id,
            max_tokens=2500,
        )
        by_id = {it.id: it for it in out.items}  # matched by id, never by position
        result: list[tuple[RawFinding, str, str]] = []
        for i, r in enumerate(raw):
            item = by_id.get(i)
            if item and item.verdict == "false_positive" and not r.deterministic:
                continue
            result.append((r, item.explanation if item else "", item.fix if item else ""))
        result.sort(key=lambda t: t[0].severity.rank, reverse=True)
        return result


def _annotation(f: RawFinding, why: str, fix: str) -> dict[str, Any]:
    level = "failure" if f.severity in (Severity.CRITICAL, Severity.HIGH) else "warning"
    msg = f"{f.message}" + (f"\n\n{why}" if why else "") + (f"\n\nFix: {fix}" if fix else "")
    return {
        "path": f.path,
        "start_line": f.line,
        "end_line": f.line,
        "annotation_level": level,
        "title": f"{f.rule} ({f.severity.value})",
        "message": msg[:60000],
    }


def _summary(items: list[tuple[RawFinding, str, str]], ran: list[str], n_files: int, skipped: int) -> str:
    head = f"Scanned {n_files} changed file(s) with: {', '.join(ran) or 'no scanners'}."
    if skipped:
        head += f" {skipped} file(s) skipped (over limit)."
    if not items:
        return head + "\n\nNo findings on changed lines."
    rows = [
        f"- {_ICON[f.severity.value]} **{f.rule}** `{f.path}:{f.line}` — {f.message}"
        for f, _, _ in items[:50]
    ]
    return head + "\n\n" + "\n".join(rows)
