#!/usr/bin/env python3
"""
trigger_itops.py — CLI script to manually trigger an IT Ops analysis.

Useful for testing the IT Ops agent without a real Azure Monitor alert.

Usage
-----
    python scripts/trigger_itops.py \
        --alert-id "test-alert-001" \
        --source "manual" \
        --log-file /path/to/log.txt

    # Or pipe log content directly:
    kubectl logs pod/worker-xxx | python scripts/trigger_itops.py \
        --alert-id "k8s-oom-$(date +%s)" \
        --source kubernetes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually trigger an IT Ops agent analysis."
    )
    parser.add_argument("--alert-id", required=True, help="Unique alert identifier")
    parser.add_argument(
        "--source",
        default="manual",
        help="Source system (default: manual)",
    )
    parser.add_argument(
        "--log-file",
        help="Path to a log file to analyse (reads stdin if omitted)",
    )
    parser.add_argument(
        "--metadata",
        help="JSON string of extra metadata (e.g. '{\"env\": \"prod\"}')",
        default="{}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print the analysis but do not trigger runbooks",
    )
    args = parser.parse_args()

    # Read log payload
    if args.log_file:
        log_payload = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
    elif not sys.stdin.isatty():
        log_payload = sys.stdin.read()
    else:
        print("❌ Provide --log-file or pipe log content via stdin.", file=sys.stderr)
        sys.exit(1)

    try:
        metadata = json.loads(args.metadata)
    except json.JSONDecodeError:
        print("❌ --metadata must be valid JSON.", file=sys.stderr)
        sys.exit(1)

    print(f"🔍 Analysing alert '{args.alert_id}' ({len(log_payload)} chars)...")

    from agents.it_ops.agent import ItOpsAgent
    from unittest.mock import patch, MagicMock

    if args.dry_run:
        # Mock runbook execution
        with patch(
            "agents.it_ops.agent.ItOpsAgent._execute_runbook", return_value=False
        ):
            agent = ItOpsAgent()
            agent.__post_init__()
            result = agent.handle_alert(
                alert_id=args.alert_id,
                source=args.source,
                log_payload=log_payload,
                metadata=metadata,
            )
    else:
        agent = ItOpsAgent()
        agent.__post_init__()
        result = agent.handle_alert(
            alert_id=args.alert_id,
            source=args.source,
            log_payload=log_payload,
            metadata=metadata,
        )

    print("\n" + "=" * 60)
    print(f"📊 RESULT")
    print("=" * 60)
    print(f"  Outcome    : {result.get('outcome', 'unknown')}")
    print(f"  Severity   : {result.get('severity', 'unknown')}")
    print(f"  Confidence : {result.get('confidence', 0):.2f}")
    print(f"  Runbook    : {result.get('runbook_name') or 'none'}")
    print()
    if result.get("ticket_draft"):
        print("📋 Ticket Draft:")
        print("-" * 60)
        print(result["ticket_draft"])
    print("=" * 60)

    sys.exit(0 if result.get("outcome") != "error" else 1)


if __name__ == "__main__":
    main()
