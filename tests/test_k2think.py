"""The K2 Think transport: what it rewrites, what it leaves alone, and that it sits in the client.

Every "blocked" case below was refused by the live endpoint's firewall with an HTML 403 during
the probe that shaped `sanitize`; every "allowed" case went through. The tests pin the rewrite,
not the firewall, which can only be sampled.
"""

from __future__ import annotations

import json

import httpx
import pytest

from coder_agent.k2think import (
    AsyncSanitizingTransport,
    SanitizingTransport,
    http_clients,
    sanitize,
)


def body(*contents: str) -> bytes:
    return json.dumps({"messages": [{"role": "user", "content": c} for c in contents]}).encode()


@pytest.mark.parametrize(
    ("blocked", "defused"),
    [
        ("run `python -m pytest -q` now", 'run "python -m pytest -q` now'),
        ("`python -c 1`", '"python -c 1`'),
        ("`Python  -m pytest`", '"Python  -m pytest`'),
        ("'python -m pytest'", '"python -m pytest\''),
        ("`perl -e 1`", '"perl -e 1`'),
        ("`curl http://x`", '"curl http://x`'),
        ("```\npython -m pytest -q\n```", "```text\npython -m pytest -q\n```"),
    ],
)
def test_quoted_interpreters_are_defused(blocked, defused):
    out = json.loads(sanitize(body(blocked)))
    assert out["messages"][0]["content"] == defused


@pytest.mark.parametrize(
    "allowed",
    [
        "see `foo.py`",
        "`ls -la`",
        "`uv run pytest`",
        "`npm test`",
        "```bash\npython -m pytest -q\n```",
        "```python\nimport os\n```",
        "python -m pytest -q",  # unquoted is fine
        'for example "python -m pytest -q"',
        "it's a python -m thing",  # the apostrophe is not in front of the interpreter
    ],
)
def test_everything_else_is_returned_untouched(allowed):
    raw = body(allowed)
    assert sanitize(raw) is raw  # identity, so the transport can skip rebuilding the request


def test_tool_descriptions_and_history_are_covered_too():
    raw = json.dumps({
        "messages": [
            {"role": "assistant", "content": "I ran `python -m pytest -q`."},
            {"role": "tool", "content": "exit code: 1\n'perl -e 1' failed"},
        ],
        "tools": [{"function": {"description": "for example `python -m pytest -q`"}}],
    }).encode()
    out = sanitize(raw).decode()
    assert "`python" not in out and "'perl" not in out
    assert json.loads(out)["tools"][0]["function"]["description"] == 'for example "python -m pytest -q`'


def test_non_utf8_bytes_survive_the_round_trip():
    raw = b'{"content": "`python -m x` \xff"}'
    assert sanitize(raw) == b'{"content": "\\"python -m x` \xff"}'  # the quote goes in escaped


def test_the_rewritten_body_is_still_json():
    raw = body("run `python -m pytest -q` and 'perl -e 1'")
    assert json.loads(sanitize(raw))["messages"][0]["content"] == (
        'run "python -m pytest -q` and "perl -e 1\''
    )


def test_a_body_that_is_not_json_is_left_alone():
    raw = b"not json at all `python -m x`"
    assert sanitize(raw) is raw


# --- Text-block lists: what the MCP adapter returns, what K2 refuses ---------------------------


def test_tool_results_as_text_blocks_become_one_string():
    raw = json.dumps({
        "messages": [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": [
                {"type": "text", "text": "exit code: 1"},
                {"type": "text", "text": "ran `python -m pytest -q`"},
            ]},
        ]
    }).encode()
    out = json.loads(sanitize(raw))["messages"]
    assert out[0]["content"] == "fix it"  # strings untouched
    assert out[2]["content"] == 'exit code: 1\nran "python -m pytest -q`'  # joined, then defused
    assert out[2]["tool_call_id"] == "1"  # the rest of the message survives


def test_only_all_text_block_lists_are_flattened():
    """A list with a non-text block is not ours to guess at; K2 will say what it thinks of it."""
    blocks = [{"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "x"}}]
    raw = json.dumps({"messages": [{"role": "user", "content": blocks}]}).encode()
    assert sanitize(raw) is raw


def _capture(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler


def test_sync_transport_rewrites_the_body_and_the_content_length():
    seen: list[httpx.Request] = []
    client = httpx.Client(transport=SanitizingTransport(httpx.MockTransport(_capture(seen))))
    payload = {"messages": [{"role": "user", "content": "`python -m pytest -q`"}]}

    resp = client.post("https://k2.example/v1/chat/completions", json=payload)

    assert resp.status_code == 200
    sent = seen[0]
    assert json.loads(sent.content)["messages"][0]["content"] == '"python -m pytest -q`'
    assert int(sent.headers["content-length"]) == len(sent.content)
    assert sent.headers["content-type"] == "application/json"


async def test_async_transport_does_the_same():
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=AsyncSanitizingTransport(httpx.MockTransport(_capture(seen)))
    )
    await client.post("https://k2.example/v1", json={"q": "'curl http://x'"})
    assert json.loads(seen[0].content)["q"] == '"curl http://x\''


def test_clean_requests_pass_through_the_original_request_object():
    seen: list[httpx.Request] = []
    client = httpx.Client(transport=SanitizingTransport(httpx.MockTransport(_capture(seen))))
    client.post("https://k2.example/v1", json={"q": "hello"})
    assert json.loads(seen[0].content) == {"q": "hello"}


def test_http_clients_are_wired_with_the_sanitizing_transports():
    clients = http_clients()
    assert isinstance(clients["http_client"]._transport, SanitizingTransport)
    assert isinstance(clients["http_async_client"]._transport, AsyncSanitizingTransport)
