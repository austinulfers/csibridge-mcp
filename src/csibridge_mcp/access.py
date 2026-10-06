"""Access control for the HTTP transport.

Over stdio the client is a program the user started, so nothing more is
needed. Over HTTP anyone who can reach the port could drive CSiBridge, so
every request must carry an access token. Tokens are issued per person by
whoever hosts the server (``csibridge-mcp token add <name>``), stored hashed,
and can be revoked without restarting the server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

TOKEN_PREFIX = "csib_"
MAX_NAME_LENGTH = 64

log = logging.getLogger("csibridge_mcp.http")


def default_tokens_path() -> Path:
    return Path.home() / ".csibridge-mcp" / "tokens.json"


def _digest(token: str) -> str:
    # Tokens are 256-bit random values, so a plain hash is enough to keep the
    # file from being a list of usable credentials.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """Named access tokens, kept hashed in a JSON file.

    The file is re-read whenever it changes on disk, so ``token add`` and
    ``token revoke`` take effect on a running server.
    """

    def __init__(self, path: Path | str | None = None, extra: dict[str, str] | None = None):
        self.path = Path(path) if path else default_tokens_path()
        self._entries: dict[str, dict] = {}  # name -> {"sha256": ..., "created": ...}
        self._stamp: tuple[int, int] | None = None
        # Tokens given on the command line or environment: honoured, never written to disk.
        self._extra = {_digest(token): name for name, token in (extra or {}).items() if token}

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            self._entries, self._stamp = {}, None
            return
        if stamp == self._stamp:
            return
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self._entries = {e["name"]: e for e in data.get("tokens", [])}
        self._stamp = stamp

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"tokens": list(self._entries.values())}, handle, indent=2)
            handle.write("\n")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)
        stat = self.path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    def __len__(self) -> int:
        self._refresh()
        return len(self._entries) + len(self._extra)

    def names(self) -> list[tuple[str, str]]:
        """``(name, created)`` for every stored token, oldest first."""
        self._refresh()
        return [(e["name"], e["created"]) for e in self._entries.values()]

    def add(self, name: str) -> str:
        """Create a token for ``name`` and return it: the only time it is shown."""
        name = name.strip()
        if not name or len(name) > MAX_NAME_LENGTH or not name.isprintable():
            raise ValueError("A token name must be 1-64 printable characters.")
        self._refresh()
        if name in self._entries:
            raise ValueError(f"A token named {name!r} already exists; revoke it first to replace it.")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._entries[name] = {
            "name": name,
            "sha256": _digest(token),
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        self._save()
        return token

    def revoke(self, name: str) -> bool:
        self._refresh()
        if name not in self._entries:
            return False
        del self._entries[name]
        self._save()
        return True

    def verify(self, token: str) -> str | None:
        """The name the token was issued to, or None."""
        self._refresh()
        wanted = _digest(token)
        found = None
        for name, entry in list(self._entries.items()) + [(n, {"sha256": d}) for d, n in self._extra.items()]:
            # Compare every entry so timing does not reveal which names exist.
            if hmac.compare_digest(entry["sha256"], wanted):
                found = name
        return found


class TokenAuth:
    """ASGI middleware: every request must carry a valid access token.

    The token is accepted as ``Authorization: Bearer <token>`` (what MCP
    clients send) or ``X-API-Key: <token>`` (what API-key style connectors,
    such as Copilot Studio's, can send). ``GET /health`` is open so tunnels and
    monitors can check liveness; it reveals nothing else.
    """

    def __init__(self, app, store: TokenStore):
        self.app = app
        self.store = store

    @staticmethod
    def _token_from(headers: Headers) -> str | None:
        authorization = headers.get("authorization", "")
        if authorization[:7].lower() == "bearer ":
            return authorization[7:].strip() or None
        return headers.get("x-api-key", "").strip() or None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        if method == "GET" and path == "/health":
            await JSONResponse({"ok": True, "service": "csibridge-mcp"})(scope, receive, send)
            return
        client = scope.get("client") or ("?", 0)
        token = self._token_from(Headers(scope=scope))
        name = self.store.verify(token) if token else None
        if name is None:
            log.warning("rejected %s %s from %s: %s", method, path, client[0], "unknown token" if token else "no token")
            response = JSONResponse(
                {"error": "unauthorized", "message": "A valid access token is required."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="csibridge-mcp"'},
            )
            await response(scope, receive, send)
            return
        log.info("%s %s %s from %s", name, method, path, client[0])
        scope.setdefault("state", {})["user"] = name
        await self.app(scope, receive, send)


def build_http_app(mcp, store: TokenStore):
    """The ASGI app for the HTTP transport: the MCP server behind token auth."""
    return TokenAuth(mcp.streamable_http_app(), store)
