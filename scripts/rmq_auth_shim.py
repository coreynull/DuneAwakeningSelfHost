#!/usr/bin/env python3
import ipaddress
import os
import pathlib
import sys
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from secret_env import read_secret_env
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from http_url import require_http_url


WORLD_UNIQUE_NAME = os.environ["WORLD_UNIQUE_NAME"]
UPSTREAM = os.environ.get("TEXT_ROUTER_AUTH_BASE", "http://text-router:8080")
MANAGEMENT_USER = os.environ.get("DUNE_RMQ_MANAGEMENT_USER", "")
MANAGEMENT_PASSWORD = read_secret_env("DUNE_RMQ_MANAGEMENT_PASSWORD")
SERVICE_USER_RE = re.compile(rf"^(sg|bgd|tr)\.{re.escape(WORLD_UNIQUE_NAME)}\.[^.]+(?:\.(game|admin))?$")
PLAYER_USER_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
# Service identities are granted without a password check. When configured, only
# accept them from these networks (the farm's Docker bridge) at the vhost check,
# which RabbitMQ sends with the client address. Unset keeps the previous behavior.
SERVICE_USER_CIDRS = [
    ipaddress.ip_network(item.strip(), strict=False)
    for item in os.environ.get("DUNE_RMQ_SERVICE_USER_CIDRS", "").split(",")
    if item.strip()
]


def service_source_allowed(form: dict[str, str]) -> bool:
    if not SERVICE_USER_CIDRS:
        return True
    raw = form.get("ip", "").strip()
    if raw.lower().startswith("::ffff:"):
        raw = raw[7:]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return any(address in network for network in SERVICE_USER_CIDRS)


# Diagnostic only: when true, player logins are still allowed exactly as before, but the
# same request is also sent to text-router in the background and its verdict is logged
# (username and verdict only, never the password). Used to judge whether player
# authentication could be enforced through text-router.
PLAYER_AUTH_SHADOW = os.environ.get("DUNE_RMQ_PLAYER_AUTH_SHADOW", "").lower() in ("1", "true", "yes", "on")


# When true, a player-ID login (the /v0/auth/user password check) is decided by
# text-router instead of being allowed unconditionally. Vhost, resource and topic
# checks for players are unchanged. Each decision is logged without the password.
PLAYER_AUTH_ENFORCE = os.environ.get("DUNE_RMQ_PLAYER_AUTH_ENFORCE", "").lower() in ("1", "true", "yes", "on")


def enforce_player_login(path: str, body: bytes, content_type: str | None, username: str) -> bytes:
    try:
        answer = post_upstream(path, body, content_type)
        verdict = answer.decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001 - fail closed
        answer, verdict = b"deny", f"deny (text-router error: {type(exc).__name__})"
    decision = answer if answer.strip().lower().startswith(b"allow") else b"deny"
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} player-auth-enforce user={username} "
          f"text-router={verdict[:80]} decision={decision.decode('utf-8', 'replace')[:40]}", file=sys.stderr, flush=True)
    return decision


def shadow_player_auth(path: str, body: bytes, content_type: str | None, username: str) -> None:
    def run() -> None:
        try:
            verdict = post_upstream(path, body, content_type).decode("utf-8", "replace").strip()[:80]
        except Exception as exc:  # noqa: BLE001 - diagnostic logging only
            verdict = f"error:{type(exc).__name__}"
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} player-auth-shadow path={path} "
              f"user={username} text-router={verdict}", file=sys.stderr, flush=True)
    threading.Thread(target=run, daemon=True).start()


def parse_form(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def post_upstream(path: str, body: bytes, content_type: str | None) -> bytes:
    req = urllib.request.Request(f"{UPSTREAM}{path}", data=body, method="POST")
    req.add_header("Content-Type", content_type or "application/x-www-form-urlencoded")
    require_http_url(req.full_url)
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "dune-rmq-auth-shim"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        form = parse_form(body)
        username = form.get("username", "")

        if self.path in {"/v0/auth/user", "/v0/auth/vhost", "/v0/auth/resource", "/v0/auth/topic"}:
            if MANAGEMENT_USER and username == MANAGEMENT_USER:
                if self.path == "/v0/auth/user" and form.get("password", "") == MANAGEMENT_PASSWORD:
                    self.respond(b"allow administrator")
                    return
                if self.path != "/v0/auth/user":
                    self.respond(b"allow")
                    return
                self.respond(b"deny")
                return
            if SERVICE_USER_RE.match(username):
                if self.path == "/v0/auth/vhost" and not service_source_allowed(form):
                    self.respond(b"deny")
                    return
                if self.path == "/v0/auth/user":
                    self.respond(b"allow administrator")
                    return
                self.respond(b"allow")
                return
            if PLAYER_USER_RE.match(username):
                if PLAYER_AUTH_ENFORCE and self.path == "/v0/auth/user":
                    self.respond(enforce_player_login(self.path, body, self.headers.get("Content-Type"), username))
                    return
                if PLAYER_AUTH_SHADOW and self.path == "/v0/auth/user":
                    shadow_player_auth(self.path, body, self.headers.get("Content-Type"), username)
                self.respond(b"allow")
                return

        try:
            self.respond(post_upstream(self.path, body, self.headers.get("Content-Type")))
        except Exception:
            self.respond(b"deny")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AuthServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = int(os.environ.get("RMQ_AUTH_SHIM_BACKLOG", "128"))


if __name__ == "__main__":
    AuthServer(("0.0.0.0", 8080), Handler).serve_forever()
