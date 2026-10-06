"""Command line entry point.

    csibridge-mcp [serve] [--mock] ...      the MCP server over stdio (default)
    csibridge-mcp serve --http ...          the MCP server over HTTP, behind access tokens
    csibridge-mcp selftest [--mock]         attach to CSiBridge and run read-only checks
    csibridge-mcp token add|list|revoke     manage access tokens for the HTTP transport
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys

from .engine import DEFAULT_HELPER_PROGID, DEFAULT_PROGID, ComBackend, Engine, MockBackend, selftest

COMMANDS = ("serve", "selftest", "token")
DEFAULT_HTTP_PORT = 8765


def _backend_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--mock",
        action="store_true",
        help="use a fake in-memory model instead of CSiBridge, for development off Windows "
        "(also enabled by CSIBRIDGE_MCP_MOCK=1)",
    )
    common.add_argument("--no-launch", action="store_true", help="never start CSiBridge, only attach to a running one")
    common.add_argument("--progid", default=DEFAULT_PROGID, help="COM ProgID of the CSiBridge application object")
    common.add_argument("--helper-progid", default=DEFAULT_HELPER_PROGID, help="COM ProgID of the CSI API helper")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csibridge-mcp",
        description="MCP server that lets an AI assistant drive a running CSiBridge through its API.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    backend = _backend_options()

    serve = commands.add_parser("serve", parents=[backend], help="run the MCP server (default command)")
    serve.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio; requires access tokens")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP: address to listen on (default: %(default)s)")
    serve.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="HTTP: port (default: %(default)s)")
    serve.add_argument("--tls-cert", help="HTTP: serve HTTPS with this certificate file (PEM)")
    serve.add_argument("--tls-key", help="HTTP: the certificate's private key file (PEM)")
    serve.add_argument("--tokens-file", help="HTTP: where access tokens are stored (default: ~/.csibridge-mcp/tokens.json)")
    serve.add_argument(
        "--token",
        action="append",
        default=[],
        help="HTTP: also accept this token (for testing; prefer 'token add'). CSIBRIDGE_MCP_TOKEN works too.",
    )
    serve.set_defaults(handler=_serve)

    check = commands.add_parser("selftest", parents=[backend], help="attach to CSiBridge, run read-only checks, print a report")
    check.add_argument("--launch", action="store_true", help="start CSiBridge if it is not running")
    check.set_defaults(handler=_selftest)

    token = commands.add_parser("token", help="manage access tokens for the HTTP transport")
    token.add_argument("--tokens-file", help="where tokens are stored (default: ~/.csibridge-mcp/tokens.json)")
    actions = token.add_subparsers(dest="action", required=True, metavar="action")
    add = actions.add_parser("add", help="create a token for a person and print it (shown only once)")
    add.add_argument("name", help="who the token is for, e.g. a name or email address")
    actions.add_parser("list", help="list token names")
    revoke = actions.add_parser("revoke", help="revoke a person's token")
    revoke.add_argument("name")
    token.set_defaults(handler=_token)
    return parser


def _engine(args: argparse.Namespace) -> Engine:
    if args.mock or os.environ.get("CSIBRIDGE_MCP_MOCK") == "1":
        backend = MockBackend()
    else:
        backend = ComBackend(args.progid, args.helper_progid)
    engine = Engine(backend, allow_launch=not args.no_launch)
    engine.start()
    return engine


def _selftest(args: argparse.Namespace) -> int:
    return selftest(_engine(args), launch=args.launch)


def _serve(args: argparse.Namespace) -> int:
    from .server import create_server

    if not args.http:
        create_server(_engine(args)).run()
        return 0

    from .access import TokenStore, build_http_app

    extra = {f"command-line token {i + 1}": token for i, token in enumerate(args.token)}
    if os.environ.get("CSIBRIDGE_MCP_TOKEN"):
        extra["CSIBRIDGE_MCP_TOKEN"] = os.environ["CSIBRIDGE_MCP_TOKEN"]
    store = TokenStore(args.tokens_file, extra)
    if len(store) == 0:
        sys.exit(
            "The HTTP transport only serves requests that carry an access token, and none exist yet.\n"
            "Create one for each person who may use the server:\n"
            "    csibridge-mcp token add <name>"
        )
    if bool(args.tls_cert) != bool(args.tls_key):
        sys.exit("--tls-cert and --tls-key must be given together.")

    logging.getLogger("csibridge_mcp").setLevel(logging.INFO)
    engine = _engine(args)
    app = build_http_app(create_server(engine, http=True), store)
    _print_http_banner(args, store)
    try:
        _run_http(app, args.host, args.port, args.tls_cert, args.tls_key)
    finally:
        engine.stop()
    return 0


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _print_http_banner(args: argparse.Namespace, store) -> None:
    from .server import MCP_PATH

    scheme = "https" if args.tls_cert else "http"
    shown_host = "localhost" if args.host in ("0.0.0.0", "::", "") else args.host
    print(f"csibridge-mcp HTTP server: {scheme}://{shown_host}:{args.port}{MCP_PATH}")
    print(f"Access tokens: {len(store)} ({store.path})")
    if _is_loopback(args.host):
        print("Listening on this machine only. To reach it from elsewhere, put a TLS tunnel or reverse proxy in front,")
        print("or listen on all interfaces with --host 0.0.0.0 (then use --tls-cert/--tls-key).")
    elif not args.tls_cert:
        print("WARNING: listening on a network interface without TLS. Tokens and model data travel unencrypted;")
        print("only do this behind a reverse proxy or tunnel that terminates HTTPS.")
    print("Press Ctrl+C to stop.", flush=True)


def _run_http(app, host: str, port: int, tls_cert: str | None, tls_key: str | None) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, ssl_certfile=tls_cert, ssl_keyfile=tls_key, log_level="info")


def _token(args: argparse.Namespace) -> int:
    from .access import TokenStore

    store = TokenStore(args.tokens_file)
    if args.action == "add":
        try:
            token = store.add(args.name)
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"Access token for {args.name} (shown once; it is stored hashed in {store.path}):")
        print()
        print(f"    {token}")
        print()
        print("Give it only to that person. They send it as 'Authorization: Bearer <token>' or 'X-API-Key: <token>'.")
        return 0
    if args.action == "revoke":
        if not store.revoke(args.name):
            sys.exit(f"No token named {args.name!r}.")
        print(f"Revoked the token for {args.name}. A running server stops accepting it immediately.")
        return 0
    names = store.names()
    if not names:
        print(f"No tokens in {store.path}. Create one with: csibridge-mcp token add <name>")
    for name, created in names:
        print(f"{name}  (created {created})")
    return 0


def normalize_argv(argv: list[str]) -> list[str]:
    """Let ``csibridge-mcp --mock`` mean ``csibridge-mcp serve --mock``."""
    if argv and (argv[0] in COMMANDS or argv[0] in ("-h", "--help")):
        return argv
    return ["serve", *argv]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    return args.handler(args)
