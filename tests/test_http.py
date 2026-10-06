"""The HTTP transport: access tokens, the auth middleware, and MCP over HTTP."""
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.responses import JSONResponse

from csibridge_mcp.access import TokenStore, build_http_app
from csibridge_mcp.cli import build_parser, normalize_argv
from csibridge_mcp.server import MCP_PATH, create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "tokens.json")


# -- the token store --------------------------------------------------------


def test_tokens_are_issued_once_and_stored_hashed(store):
    token = store.add("alice@example.com")
    assert token.startswith("csib_") and len(token) > 40
    assert store.verify(token) == "alice@example.com"
    assert store.verify("csib_not-a-real-token") is None
    assert token not in store.path.read_text()
    assert [name for name, _ in store.names()] == ["alice@example.com"]
    assert len(store) == 1


def test_duplicate_and_invalid_names_are_refused(store):
    store.add("alice")
    with pytest.raises(ValueError, match="already exists"):
        store.add("alice")
    with pytest.raises(ValueError, match="1-64 printable"):
        store.add("   ")


def test_revocation_is_seen_by_a_running_server(tmp_path):
    admin = TokenStore(tmp_path / "tokens.json")
    server_side = TokenStore(tmp_path / "tokens.json")  # the store a running server holds
    token = admin.add("bob")
    assert server_side.verify(token) == "bob"
    assert admin.revoke("bob") is True
    assert server_side.verify(token) is None
    assert admin.revoke("bob") is False


def test_command_line_tokens_are_honoured_but_never_saved(tmp_path):
    store = TokenStore(tmp_path / "tokens.json", extra={"ci": "csib_cli-token"})
    assert store.verify("csib_cli-token") == "ci"
    assert len(store) == 1
    assert not store.path.exists()


# -- the middleware ---------------------------------------------------------


async def _echo_user(scope, receive, send):
    await JSONResponse({"user": scope["state"]["user"]})(scope, receive, send)


async def test_requests_need_a_token(store):
    token = store.add("alice")
    from csibridge_mcp.access import TokenAuth

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=TokenAuth(_echo_user, store)), base_url="http://t") as client:
        assert (await client.get("/health")).json() == {"ok": True, "service": "csibridge-mcp"}

        refused = await client.post(MCP_PATH)
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"].startswith("Bearer")
        assert (await client.post(MCP_PATH, headers={"Authorization": "Bearer csib_wrong"})).status_code == 401

        as_bearer = await client.post(MCP_PATH, headers={"Authorization": f"Bearer {token}"})
        assert as_bearer.json() == {"user": "alice"}
        as_api_key = await client.post(MCP_PATH, headers={"X-API-Key": token})
        assert as_api_key.json() == {"user": "alice"}


# -- MCP over HTTP, in process ----------------------------------------------


async def test_mcp_session_over_http(engine, store):
    token = store.add("alice")
    mcp = create_server(engine, http=True)
    app = build_http_app(mcp, store)

    def in_process_client(headers=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers)

    async with mcp.session_manager.run():
        url = f"http://t{MCP_PATH}"
        async with in_process_client({"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "csibridge"
                    result = await session.call_tool("status", {})
                    assert json.loads(result.content[0].text)["backend"] == "mock"

        with pytest.raises(Exception):  # no token: the server answers 401 before MCP is reached
            async with in_process_client() as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()


# -- the real thing: the CLI serving over a socket --------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_cli_serves_http_end_to_end(tmp_path):
    port = _free_port()
    command = [
        sys.executable, "-m", "csibridge_mcp", "serve", "--http", "--mock",
        "--port", str(port), "--token", "csib_test", "--tokens-file", str(tmp_path / "tokens.json"),
    ]  # fmt: skip
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert process.poll() is None, "server exited early:\n" + process.stdout.read()
            assert time.time() < deadline, "server did not come up"
            time.sleep(0.1)

        async with httpx.AsyncClient(headers={"X-API-Key": "csib_test"}) as http:
            async with streamable_http_client(f"{url}{MCP_PATH}", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 15
                    result = await session.call_tool("call", {"method": "SapModel.FrameObj.Count"})
                    assert json.loads(result.content[0].text)["ret"] == 0
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate()[0]
    assert "csibridge-mcp HTTP server: http://127.0.0.1" in output
    assert "command-line token 1 POST /mcp" in output  # requests are logged by token name


def test_http_refuses_to_start_without_tokens(tmp_path):
    env = {**os.environ, "CSIBRIDGE_MCP_TOKEN": ""}
    command = [sys.executable, "-m", "csibridge_mcp", "serve", "--http", "--mock", "--tokens-file", str(tmp_path / "none.json")]
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode != 0
    assert "csibridge-mcp token add <name>" in result.stderr


# -- command line -----------------------------------------------------------


def test_serve_is_the_default_command():
    assert normalize_argv(["--mock"]) == ["serve", "--mock"]
    assert normalize_argv([]) == ["serve"]
    assert normalize_argv(["selftest", "--mock"]) == ["selftest", "--mock"]
    assert normalize_argv(["--help"]) == ["--help"]
    args = build_parser().parse_args(normalize_argv(["--mock"]))
    assert (args.command, args.mock, args.http) == ("serve", True, False)


def test_token_commands(tmp_path, capsys):
    from csibridge_mcp.cli import main

    path = str(tmp_path / "tokens.json")
    assert main(["token", "--tokens-file", path, "add", "carol"]) == 0
    token = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("csib_")][0]
    assert TokenStore(path).verify(token) == "carol"

    assert main(["token", "--tokens-file", path, "list"]) == 0
    assert "carol" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        main(["token", "--tokens-file", path, "revoke", "nobody"])
    assert main(["token", "--tokens-file", path, "revoke", "carol"]) == 0
    assert TokenStore(path).verify(token) is None
