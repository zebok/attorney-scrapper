"""CLI interface for the Texas Bar Attorney Enrichment tool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from txbar.client import TexasBarClient
from txbar.config import get_settings
from txbar.pipeline import Pipeline
from txbar.storage import get_storage


def cmd_health(args: argparse.Namespace) -> None:
    settings = get_settings()
    with TexasBarClient(settings) as client:
        res = client.health_check()
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("status") == "healthy" else 1)


def cmd_seed(args: argparse.Namespace) -> None:
    settings = get_settings()
    storage = get_storage(settings)
    csv_path = Path(args.csv) if args.csv else settings.resolve(Path("data/input/tx_lawyers.csv"))
    if not csv_path.exists():
        print(f"Error: input file '{csv_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    count = storage.seed_source(csv_path)
    print(f"Successfully seeded {count} attorneys into source table.")


def cmd_run(args: argparse.Namespace) -> None:
    settings = get_settings()
    pipeline = Pipeline(settings=settings)
    res = pipeline.run(limit=args.limit)
    print("\n================ RUN SUMMARY ================")
    print(f"Run ID:         {res.get('run_id')}")
    print(f"Status:         {res.get('status')}")
    print(f"Total:          {res.get('total')}")
    print(f"Auto-Approved:  {res.get('auto_approved')}")
    print(f"Pending Review: {res.get('pending_review')}")
    print(f"Not Found:      {res.get('not_found')}")
    print(f"Firm Changes:   {res.get('firm_changes')}")
    print(f"DQ Gate Passed: {res.get('dq_gate_passed')}")
    print("=============================================\n")


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def cmd_ui(args: argparse.Namespace) -> None:
    import os
    import uvicorn

    from txbar.web.app import app

    port = args.port
    is_random = False
    if not port or port == 0:
        # Check if running in Cloud Run container with $PORT defined
        env_port = os.getenv("PORT")
        if env_port:
            port = int(env_port)
        else:
            port = _find_free_port()
            is_random = True

    host = args.host
    port_label = f"(randomly assigned: {port})" if is_random else f":{port}"
    print(f"\n🚀 Starting Texas Bar Review Console on http://{host}:{port} {port_label}")
    print(f"👉 Open in browser: http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}\n")
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser(prog="txbar", description="Texas Bar Attorney Enrichment CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Health
    p_health = subparsers.add_parser("health", help="Check Texas Bar directory connectivity and DOM structure")
    p_health.set_defaults(func=cmd_health)

    # Seed
    p_seed = subparsers.add_parser("seed", help="Seed source table from input CSV")
    p_seed.add_argument("--csv", type=str, default=None, help="Path to input CSV")
    p_seed.set_defaults(func=cmd_seed)

    # Run
    p_run = subparsers.add_parser("run", help="Execute the enrichment pipeline")
    p_run.add_argument("--limit", type=int, default=None, help="Limit number of attorneys to process (for testing)")
    p_run.set_defaults(func=cmd_run)

    # UI
    p_ui = subparsers.add_parser("ui", help="Launch the Human-in-the-Loop Review UI and API")
    p_ui.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default 127.0.0.1)")
    p_ui.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to listen on (default: 0 for dynamic random free port)",
    )
    p_ui.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
