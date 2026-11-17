"""Guard rails on the GitHub Actions workflows (supply chain and secret handling)."""

from __future__ import annotations

import re

import pytest
import yaml

from tests.conftest import ROOT

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SHA = re.compile(r"@[0-9a-f]{40}(\s|$)")


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def all_steps(wf):
    for job in (wf.get("jobs") or {}).values():
        yield from job.get("steps") or []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_third_party_action_is_pinned_to_a_commit_sha(path):
    for step in all_steps(load(path)):
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert SHA.search(uses + " "), f"{path.name}: {uses} is not pinned to a full commit SHA"


def ai_review():
    return load(ROOT / ".github" / "workflows" / "ai-review.yml")


def test_the_model_key_is_never_the_github_token():
    """If the provider key were missing, falling back to GITHUB_TOKEN would send it to the model provider."""
    env_blocks = [s.get("env") or {} for s in all_steps(ai_review())]
    key = [e["DA_LLM_API_KEY"] for e in env_blocks if "DA_LLM_API_KEY" in e]
    assert key, "the review step must set DA_LLM_API_KEY"
    assert all("GITHUB_TOKEN" not in k for k in key)
    assert all("AI_LLM_API_KEY" in k for k in key)


def test_the_github_token_only_reaches_the_github_client():
    for env in (s.get("env") or {} for s in all_steps(ai_review())):
        for name, value in env.items():
            if "GITHUB_TOKEN" in str(value):
                assert name == "DA_GITHUB_TOKEN", f"{name} must not carry the GitHub token"


def test_the_retired_models_permission_is_gone_and_permissions_stay_minimal():
    perms = ai_review()["permissions"]
    assert "models" not in perms  # GitHub Models was retired on 2026-07-30
    assert set(perms) <= {"contents", "pull-requests", "checks"}
    assert perms["contents"] == "read"


def test_the_review_workflow_never_checks_out_pull_request_code():
    assert not [s for s in all_steps(ai_review()) if str(s.get("uses", "")).startswith("actions/checkout")]
