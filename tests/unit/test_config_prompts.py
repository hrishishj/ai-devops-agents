import pytest

from devopsagents.core.config.repo_config import ReviewMode, parse_repo_config
from devopsagents.core.prompts.registry import PromptNotFound, PromptRegistry


def test_repo_config_defaults_to_shadow():
    assert parse_repo_config(None).review.mode == ReviewMode.SHADOW


def test_repo_config_valid_and_invalid():
    cfg = parse_repo_config("version: 1\nreview:\n  mode: comment\n  max_comments: 5\n")
    assert cfg.review.mode == ReviewMode.COMMENT and cfg.review.max_comments == 5
    # invalid values fall back to safe defaults rather than raising
    assert parse_repo_config("review: {mode: yolo}").review.mode == ReviewMode.SHADOW
    assert parse_repo_config(": : :").review.mode == ReviewMode.SHADOW


def test_prompt_registry_latest_and_pinned(tmp_path):
    d = tmp_path / "review"
    d.mkdir()
    (d / "x.v1.md").write_text("one")
    (d / "x.v2.md").write_text("two")
    reg = PromptRegistry(tmp_path)
    assert reg.get("review", "x").id == "review/x.v2" and reg.get("review", "x").text == "two"
    assert reg.get("review", "x", 1).text == "one"
    with pytest.raises(PromptNotFound):
        reg.get("review", "nope")
    with pytest.raises(PromptNotFound):
        reg.get("review", "x", 9)
