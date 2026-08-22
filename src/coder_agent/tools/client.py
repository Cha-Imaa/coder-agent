"""MCP client: launches the tool server and exposes its tools as LangChain tools.

`MultiServerMCPClient` can talk to several servers at once (ours, plus later a GitHub or web-search
server). For each server it performs the MCP handshake, calls `tools/list`, and wraps every tool
in a LangChain `BaseTool` whose `args_schema` comes from the server's JSON schema. LangGraph's
`ToolNode` can then call them like any other tool.

Transport is stdio: the client spawns the server as a child process and exchanges JSON-RPC
messages over its stdin/stdout. Nothing touches the network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient


def server_config(repo: Path) -> dict:
    """Connection spec for our own server. Uses the current interpreter so the venv is respected.

    The server is its own process with its own `Settings`, read from the environment. A flag
    such as `--sandbox docker` changes the client's settings only, so the values that matter are
    mirrored into the child's environment on top of the parent's (a partial `env` would replace
    the whole environment, losing PATH and the API keys).
    """
    from coder_agent.sandbox import env_overrides

    return {
        "coder-tools": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "coder_agent.mcp_server.server", str(repo.resolve())],
            "env": {**os.environ, **env_overrides()},
        }
    }


def strict_arguments(tool: BaseTool) -> BaseTool:
    """Reject calls whose argument names are not in the tool's schema.

    Models invent plausible names (`line_start` for `start_line`, `depth` for `max_depth`).
    Both MCP and pydantic ignore unknown keys by default, so such a call silently runs with the
    default values, and a request for lines 50-80 quietly returns the whole file. Returning an
    error that names the valid arguments lets the model correct itself on the next step.
    """
    schema = tool.args_schema
    if not isinstance(schema, dict) or not isinstance(tool, StructuredTool) or not tool.coroutine:
        return tool
    allowed = set(schema.get("properties", {}))
    inner = tool.coroutine

    async def checked(**kwargs: Any) -> Any:
        unknown = sorted(set(kwargs) - allowed)
        if unknown:
            message = (
                f"ERROR: unknown argument(s) {unknown} for {tool.name}. "
                f"Valid arguments: {sorted(allowed)}."
            )
            # Adapter tools use response_format="content_and_artifact": (content blocks, raw).
            return [{"type": "text", "text": message}], None
        return await inner(**kwargs)

    tool.coroutine = checked
    return tool


async def load_tools(repo: Path) -> list[BaseTool]:
    """Discover and return the tools for `repo`."""
    client = MultiServerMCPClient(server_config(repo))
    return [strict_arguments(t) for t in await client.get_tools()]
