"""Evaluation harness for prompts/models.

    python -m evals.run_evals --offline          # CI: validates the harness with recorded replies
    python -m evals.run_evals                    # live: uses DA_OPENAI_ENDPOINT (Azure OpenAI)

Prompt or model changes must not regress the thresholds below (see docs/slo.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devopsagents.agents.incident.workflow import IncidentAnalysis
from devopsagents.agents.review.workflow import ReviewAgent
from devopsagents.core.config.settings import Settings
from devopsagents.core.jobs.store import JobStore, make_engine
from devopsagents.core.llm.fake import ScriptedBackend
from devopsagents.core.llm.gateway import LLMBackend, LLMGateway, ModelTier
from devopsagents.core.prompts.registry import PromptRegistry
from devopsagents.domain.events import AlertEvent
from devopsagents.domain.jobs import AgentKind
from devopsagents.integrations.github.client import PullRequestInfo, ReviewComment

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "datasets"


class _ShadowGitHub:
    """Serves a recorded diff; review runs in shadow mode so nothing is ever posted."""

    def __init__(self, diff: str) -> None:
        self._diff = diff

    def get_pr(self, inst: int, repo: str, number: int) -> PullRequestInfo:
        return PullRequestInfo(repo, number, "eval", "", "sha", "base")

    def get_diff(self, inst: int, repo: str, number: int) -> str:
        return self._diff

    def get_file(self, inst: int, repo: str, path: str, ref: str) -> str | None:
        return "review:\n  mode: shadow\n  min_confidence: 0.6\n"

    def post_review(
        self, inst: int, repo: str, number: int, commit_sha: str, body: str, comments: list[ReviewComment]
    ) -> int:  # pragma: no cover
        raise AssertionError("evals must never post")


@dataclass
class Scores:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    candidates: int = 0
    grounded: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def grounding(self) -> float:
        return self.grounded / self.candidates if self.candidates else 1.0


def eval_review(make_backend: Any, store: JobStore) -> Scores:
    cases = json.loads((DATA / "review" / "cases.json").read_text(encoding="utf-8"))
    s = Scores()
    for case in cases:
        backend = make_backend(case["offline_reply"])
        agent = ReviewAgent(
            _ShadowGitHub(case["diff"]), LLMGateway(backend, store), PromptRegistry(ROOT / "prompts"), store
        )
        job, _ = store.enqueue(
            AgentKind.REVIEW,
            f"eval/{case['name']}#1",
            "sha",
            "eval",
            {"repo": "eval/r", "number": 1, "head_sha": "sha", "installation_id": 0},
        )
        leased = store.lease(job.id)
        assert leased is not None
        res = agent.handle(leased)
        got = {(c["path"], c["line"]) for c in res["comments"]}
        want = {(e["path"], e["line"]) for e in case["expect"]}
        matched = {g for g in got if any(g[0] == w[0] and abs(g[1] - w[1]) <= 1 for w in want)}
        s.tp += len(matched)
        s.fp += len(got - matched)
        s.fn += len([w for w in want if not any(w[0] == g[0] and abs(g[1] - w[1]) <= 1 for g in got)])
        s.candidates += res["candidates"]
        s.grounded += res["grounded"]
    return s


def eval_incident(make_backend: Any, store: JobStore) -> tuple[int, int]:
    cases = json.loads((DATA / "incident" / "cases.json").read_text(encoding="utf-8"))
    prompt = PromptRegistry(ROOT / "prompts").get("incident", "analyse")
    ok = 0
    for case in cases:
        gw = LLMGateway(make_backend(case["offline_reply"]), store)
        alert = AlertEvent.model_validate(case["alert"])
        ev = "\n".join(f"[{i}] {e}" for i, e in enumerate(case["evidence"])) or "(none collected)"
        out = gw.structured(
            tier=ModelTier.LARGE,
            system=prompt.text,
            user=f"<alert>\ntitle: {alert.title}\nresource: {alert.resource_id}\n"
            f"environment: {alert.environment}\n</alert>\n<evidence>\n{ev}\n</evidence>",
            response_model=IncidentAnalysis,
            scope="eval",
            prompt_id=prompt.id,
            cache=False,
        )
        ok += out.recommended_runbook == case["expected_runbook"]
    return ok, len(cases)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use recorded replies (no model calls)")
    ap.add_argument("--min-precision", type=float, default=0.8)
    ap.add_argument("--min-recall", type=float, default=0.8)
    ap.add_argument("--min-grounding", type=float, default=0.95)
    ap.add_argument("--min-incident-accuracy", type=float, default=0.8)
    args = ap.parse_args(argv)

    store = JobStore(make_engine("sqlite+pysqlite://"))
    if args.offline:

        def make_backend(reply: dict[str, object]) -> LLMBackend:
            return ScriptedBackend(responder=lambda s, u: reply)
    else:  # pragma: no cover - live model
        from devopsagents.integrations.azure.openai_backend import AzureOpenAIBackend

        st = Settings()
        live = AzureOpenAIBackend(st.openai_endpoint, st.openai_api_version, st.model_large, st.model_small)

        def make_backend(reply: dict[str, object]) -> LLMBackend:
            return live

    r = eval_review(make_backend, store)
    ok, total = eval_incident(make_backend, store)
    inc_acc = ok / total
    print(f"review   precision={r.precision:.2f} recall={r.recall:.2f} grounding={r.grounding:.2f}")
    print(f"incident runbook accuracy={inc_acc:.2f} ({ok}/{total})")

    failures = [
        name
        for name, val, floor in (
            ("precision", r.precision, args.min_precision),
            ("recall", r.recall, args.min_recall),
            ("grounding", r.grounding, args.min_grounding),
            ("incident accuracy", inc_acc, args.min_incident_accuracy),
        )
        if val < floor
    ]
    if failures:
        print("FAILED thresholds:", ", ".join(failures), file=sys.stderr)
        return 1
    print("all thresholds met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
