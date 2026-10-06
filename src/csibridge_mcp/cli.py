"""Command line entry point: run the MCP server, or the self-test."""
from __future__ import annotations

import argparse
import os

from .engine import DEFAULT_HELPER_PROGID, DEFAULT_PROGID, ComBackend, Engine, MockBackend, selftest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="csibridge-mcp",
        description="MCP server that lets Claude Code drive a running CSiBridge through its API.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "selftest"],
        default="serve",
        help="serve: run the MCP server over stdio (default). "
        "selftest: attach to CSiBridge, run read-only checks and print a report.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use a fake in-memory model instead of CSiBridge, for development off Windows "
        "(also enabled by CSIBRIDGE_MCP_MOCK=1)",
    )
    parser.add_argument("--no-launch", action="store_true", help="never start CSiBridge, only attach to a running one")
    parser.add_argument("--launch", action="store_true", help="with selftest: start CSiBridge if it is not running")
    parser.add_argument("--progid", default=DEFAULT_PROGID, help="COM ProgID of the CSiBridge application object")
    parser.add_argument("--helper-progid", default=DEFAULT_HELPER_PROGID, help="COM ProgID of the CSI API helper")
    args = parser.parse_args(argv)

    if args.mock or os.environ.get("CSIBRIDGE_MCP_MOCK") == "1":
        backend = MockBackend()
    else:
        backend = ComBackend(args.progid, args.helper_progid)
    engine = Engine(backend, allow_launch=not args.no_launch)
    engine.start()
    if args.command == "selftest":
        return selftest(engine, launch=args.launch)

    from .server import create_server

    create_server(engine).run()
    return 0
