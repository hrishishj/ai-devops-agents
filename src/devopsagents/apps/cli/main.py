"""`devopsagents` command-line tools."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _check_config(base: Path) -> int:
    """Validate runbooks, policy and prompts — run in CI so bad config never ships."""
    from devopsagents.core.config.settings import Settings
    from devopsagents.core.policy.engine import PolicyConfig
    from devopsagents.core.policy.runbooks import RunbookRegistry
    from devopsagents.core.prompts.registry import PromptRegistry

    s = Settings()
    reg = RunbookRegistry.load(base / s.runbooks_dir)
    PolicyConfig.load(base / s.policy_file)
    prompts = PromptRegistry(base / s.prompts_dir)
    for agent, name in (("review", "discover"), ("security", "triage"), ("incident", "analyse"), ("security", "testgen")):
        prompts.get(agent, name)
    print(f"ok: {len(reg.names())} runbooks, policy, prompts valid")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="devopsagents")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config", help="validate runbooks, policy and prompts")
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    sub.add_parser("serve", help="run the ingress API").add_argument("--port", type=int, default=8000)
    w = sub.add_parser("worker", help="run a worker")
    w.add_argument("--agent", action="append", choices=["review", "security", "incident"])
    r = sub.add_parser("review", help="enqueue a review+security run for an existing PR")
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--installation", type=int, required=True)
    args = p.parse_args(argv)

    if args.cmd == "check-config":
        return _check_config(Path.cwd())
    if args.cmd == "migrate":  # pragma: no cover
        from devopsagents.core.config.settings import Settings
        from devopsagents.core.jobs.migrate import upgrade

        upgrade(Settings().database_url)
        print("migrated to head")
        return 0
    if args.cmd == "serve":  # pragma: no cover
        import uvicorn

        uvicorn.run(
            "devopsagents.apps.api.main:factory",
            factory=True,
            host="0.0.0.0",  # noqa: S104
            port=args.port,
        )
        return 0
    if args.cmd == "worker":  # pragma: no cover
        from devopsagents.apps.worker.main import main as worker_main

        worker_main(args.agent)
        return 0
    if args.cmd == "review":  # pragma: no cover
        from devopsagents.agents.review.workflow import AGENT_VERSION as RV
        from devopsagents.agents.security.workflow import AGENT_VERSION as SV
        from devopsagents.apps.container import build_container
        from devopsagents.apps.dispatch import dispatch
        from devopsagents.core.config.settings import get_settings
        from devopsagents.domain.jobs import AgentKind

        c = build_container(get_settings(), base_dir=Path.cwd())
        if c.github is None:
            print("GitHub App is not configured", file=sys.stderr)
            return 2
        pr = c.github.get_pr(args.installation, args.repo, args.pr)
        data: dict[str, object] = {
            "repo": args.repo,
            "number": args.pr,
            "head_sha": pr.head_sha,
            "installation_id": args.installation,
        }
        for agent, v in ((AgentKind.REVIEW, RV), (AgentKind.SECURITY, SV)):
            job, created = dispatch(c, agent, f"{args.repo}#{args.pr}", pr.head_sha, v, data)
            print(f"{agent.value}: job {job.id} ({'queued' if created else 'already exists'})")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
