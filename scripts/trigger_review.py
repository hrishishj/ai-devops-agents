#!/usr/bin/env python3
"""
trigger_review.py — CLI script to manually trigger an AI code review on any PR.

Useful for testing the Coding Co-Pilot or re-running a review without
pushing a new commit.

Usage
-----
    python scripts/trigger_review.py \
        --repo org/repo \
        --pr 42

    # Also run security scan:
    python scripts/trigger_review.py --repo org/repo --pr 42 --security
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually trigger an AI code review on a GitHub PR."
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository in org/name format (e.g. myorg/myrepo)",
    )
    parser.add_argument(
        "--pr",
        required=True,
        type=int,
        help="Pull request number",
    )
    parser.add_argument(
        "--security",
        action="store_true",
        help="Also run the Test & Security agent",
    )
    args = parser.parse_args()

    print(f"🤖 Triggering AI Code Review for {args.repo}#{args.pr}...")

    from agents.coding_copilot.agent import CodingCopilotAgent
    agent = CodingCopilotAgent()
    agent.__post_init__()
    agent.handle_pr(args.repo, args.pr)
    print("✅ Code review complete — check the PR for inline comments.")

    if args.security:
        print(f"\n🛡️  Running Test & Security scan for {args.repo}#{args.pr}...")
        from agents.test_security.agent import TestSecurityAgent
        sec_agent = TestSecurityAgent()
        sec_agent.__post_init__()
        sec_agent.handle_pr(args.repo, args.pr)
        print("✅ Security scan complete — check the PR for the security report comment.")


if __name__ == "__main__":
    main()
