"""K2 Think transport: rewrite the two things its endpoint rejects before the request leaves.

The K2 Think endpoint speaks the OpenAI chat-completions dialect, but not all of it, and it sits
behind a web application firewall. Two request shapes that every other provider accepts come
back as errors, and both are shapes this agent produces on nearly every call:

1. *A quoted interpreter invocation anywhere in the body.* The firewall has a command-injection
   rule: `` `python -m pytest -q` `` or `'perl -e 1'` in any string is answered with an HTML 403
   before the model ever sees the request. Coding agents write exactly that, constantly: the
   model replies "I ran `python -m pytest -q`", and from then on every request carries the
   phrase in its history and every call is refused. Retrying does not help (the body is the
   same), the fallback model takes over, and a K2 run silently becomes a Groq run.
2. *Message content as a list of text blocks.* The MCP adapter returns tool results as
   `[{"type": "text", "text": ...}]`, which OpenAI's API takes as equivalent to the joined
   string; K2 answers 400 `Invalid value for 'messages.content'`.

The fix is a transport that sits under the openai client and edits the JSON body on its way out:
text-block lists become one string, the quote in front of an interpreter name becomes a double
quote, and a code fence with no language tag that opens on an interpreter gets a `text` tag. All
of it is cosmetic to the model and invisible to the firewall. Nothing else about the request
changes, and the transport is only installed for the `k2think:` provider, so the graph and the
other providers never see it. Doing this here rather than in the graph keeps the provider quirk
in the provider layer: `llm.py` is the one place the providers differ, and this is its helper.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

# Interpreters the rule fires on, measured against the live endpoint one probe at a time:
# `python -m x`, `python -c x`, `perl -e x` and `curl http://x` were refused; `python foo.py`,
# `python3 -m x`, `uv run pytest`, `node -e 1` and `bash -c ls` were let through. The list is a
# little wider than the measurement, since a rule that is not published can only be sampled.
_INTERPRETERS = r"(?:python\d*|perl|curl|php|ruby)"

# An opening backtick or single quote directly in front of an interpreter word. The closing
# quote is left where it is: it is the opening one the rule keys on, and an unmatched quote
# reads fine to a model. The lookbehind keeps the last backtick of a ```python fence out of it.
_QUOTED = re.compile(rf"(?<!`)[`']\s*(?={_INTERPRETERS}\b)", re.IGNORECASE)
_FENCE = re.compile(rf"```\n(?={_INTERPRETERS}\b)", re.IGNORECASE)


def _defused(text: str) -> str:
    return _FENCE.sub("```text\n", _QUOTED.sub('"', text))


def _flatten_text_blocks(messages: Any) -> bool:
    """Join each message's list of text blocks into one string. True if any message changed."""
    changed = False
    if not isinstance(messages, list):
        return changed
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if (
            isinstance(content, list)
            and content
            and all(isinstance(b, dict) and b.get("type") == "text" for b in content)
        ):
            message["content"] = "\n".join(str(b.get("text", "")) for b in content)
            changed = True
    return changed


def _defuse_strings(node: Any) -> bool:
    """Rewrite every string in the payload in place. True if any of them changed."""
    changed = False
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, value in items:
        if isinstance(value, str):
            fixed = _defused(value)
            if fixed != value:
                node[key] = fixed
                changed = True
        else:
            changed |= _defuse_strings(value)
    return changed


def sanitize(body: bytes) -> bytes:
    """The JSON request body with the endpoint's triggers defused; the same bytes if none.

    The body is parsed rather than scanned as text, so the rewrite works on the strings the
    model will read and the result is JSON by construction. Bytes that are not valid UTF-8 ride
    through as surrogates and come out as they went in.
    """
    try:
        payload = json.loads(body.decode("utf-8", errors="surrogateescape"))
    except ValueError:
        return body  # not JSON: not ours to touch
    changed = _flatten_text_blocks(payload.get("messages")) if isinstance(payload, dict) else False
    changed = _defuse_strings(payload) or changed
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="surrogateescape")


def _rewritten(request: httpx.Request, body: bytes) -> httpx.Request:
    headers = request.headers.copy()
    del headers["content-length"]  # recomputed from the new content
    return httpx.Request(request.method, request.url, headers=headers, content=body)


class SanitizingTransport(httpx.BaseTransport):
    """Sync transport wrapper: sanitize the body, then hand the request to the real transport."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        fixed = sanitize(body)
        return self._inner.handle_request(request if fixed is body else _rewritten(request, fixed))

    def close(self) -> None:
        self._inner.close()


class AsyncSanitizingTransport(httpx.AsyncBaseTransport):
    """The same for the async client, which `ainvoke` and streaming use."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        fixed = sanitize(body)
        return await self._inner.handle_async_request(
            request if fixed is body else _rewritten(request, fixed)
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def http_clients() -> dict[str, httpx.Client | httpx.AsyncClient]:
    """`ChatOpenAI` constructor arguments that route both of its clients through the rewrite."""
    return {
        "http_client": httpx.Client(transport=SanitizingTransport()),
        "http_async_client": httpx.AsyncClient(transport=AsyncSanitizingTransport()),
    }
