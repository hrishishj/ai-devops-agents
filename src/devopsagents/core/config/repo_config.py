"""Per-repository `.ai-agents.yml` — validated, with safe defaults."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class ReviewMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"  # run + store, never post
    COMMENT = "comment"


class ReviewConfig(BaseModel):
    mode: ReviewMode = ReviewMode.SHADOW  # safe default: shadow until a repo opts in
    paths_ignore: list[str] = Field(
        default_factory=lambda: [
            "**/*.lock",
            "**/package-lock.json",
            "**/generated/**",
            "**/*.min.js",
            "**/vendor/**",
        ]
    )
    max_comments: int = Field(default=15, ge=1, le=100)
    min_confidence: float = Field(default=0.6, ge=0, le=1)


class SecurityConfig(BaseModel):
    enabled: bool = True
    fail_on: list[Literal["critical", "high", "medium", "low"]] = ["critical"]
    scanners: list[str] = Field(default_factory=lambda: ["secrets", "semgrep"])
    paths_ignore: list[str] = Field(default_factory=lambda: ["tests/**", "**/fixtures/**"])


class RepoConfig(BaseModel):
    version: Literal[1] = 1
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)


def parse_repo_config(text: str | None) -> RepoConfig:
    """Invalid or missing config falls back to safe defaults (shadow mode); never raises."""
    if not text:
        return RepoConfig()
    try:
        data = yaml.safe_load(text) or {}
        return RepoConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError, TypeError):
        return RepoConfig()
