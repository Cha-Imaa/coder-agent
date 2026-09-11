"""A stand-in for api.github.com on localhost.

Serves the three endpoints the GitHub tool server uses (repository, issue, issue comments) and
accepts pull-request creation, recording what was posted. Pointing `GITHUB_API_URL` at it lets
the real MCP server run over stdio in tests with no network and no token of anyone's.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.comments: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self.default_branches: dict[tuple[str, str], str] = {}
        self.pull_requests: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []  # (method, path, headers)
        self.require_token: str | None = None  # when set, writes need this bearer token
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> FakeGitHub:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- fixtures ---------------------------------------------------------------------------

    def add_issue(
        self, owner: str, repo: str, number: int, title: str, body: str = "",
        labels: tuple[str, ...] = (), comments: tuple[tuple[str, str], ...] = (),
        author: str = "reporter", default_branch: str = "main",
    ) -> None:
        self.issues[(owner, repo, number)] = {
            "number": number, "title": title, "body": body, "state": "open",
            "user": {"login": author}, "labels": [{"name": lb} for lb in labels],
            "html_url": f"https://github.com/{owner}/{repo}/issues/{number}",
            "comments": len(comments),
        }
        self.comments[(owner, repo, number)] = [
            {"user": {"login": who}, "body": text} for who, text in comments
        ]
        self.default_branches[(owner, repo)] = default_branch

    # -- handler ----------------------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:  # keep pytest output clean
                pass

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                fake.requests.append(("GET", path, dict(self.headers)))
                if m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/(\d+)/comments", path):
                    key = (m[1], m[2], int(m[3]))
                    if key in fake.comments:
                        return self._send(200, fake.comments[key])
                elif m := re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/(\d+)", path):
                    key = (m[1], m[2], int(m[3]))
                    if key in fake.issues:
                        return self._send(200, fake.issues[key])
                elif m := re.fullmatch(r"/repos/([^/]+)/([^/]+)", path):
                    if (m[1], m[2]) in fake.default_branches:
                        return self._send(200, {"default_branch": fake.default_branches[(m[1], m[2])]})
                self._send(404, {"message": "Not Found"})

            def do_POST(self) -> None:
                path = self.path.split("?")[0]
                headers = dict(self.headers)
                fake.requests.append(("POST", path, headers))
                if fake.require_token and headers.get("Authorization") != f"Bearer {fake.require_token}":
                    return self._send(401, {"message": "Bad credentials"})
                m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/pulls", path)
                if not m:
                    return self._send(404, {"message": "Not Found"})
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
                missing = [k for k in ("title", "head", "base") if not data.get(k)]
                if missing:
                    return self._send(422, {
                        "message": "Validation Failed",
                        "errors": [{"code": "missing_field", "field": f} for f in missing],
                    })
                number = 100 + len(fake.pull_requests)
                pr = {
                    **data, "owner": m[1], "repo": m[2], "number": number, "state": "open",
                    "html_url": f"https://github.com/{m[1]}/{m[2]}/pull/{number}",
                }
                fake.pull_requests.append(pr)
                self._send(201, pr)

        return Handler
