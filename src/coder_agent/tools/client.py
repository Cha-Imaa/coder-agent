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

# GitHub tools that change things outside the repository. The server exposes them for other
# clients (`coder fix-issue` itself, an IDE); the model does not get them. It cannot push (the
# sandbox denylist), so it could never open a valid pull request anyway, and outward-facing
# actions are the user's to take. Which tools a model sees is client policy, not server policy.
GITHUB_WRITE_TOOLS = frozenset({"open_pull_request"})


def server_config(repo: Path, github: bool = False) -> dict:
    """Connection specs for our servers. Uses the current interpreter so the venv is respected.

    Each server is its own process with its own `Settings`, read from the environment. A flag
    such as `--sandbox docker` changes the client's settings only, so the values that matter are
    mirrored into the child's environment on top of the parent's (a partial `env` would replace
    the whole environment, losing PATH and the API keys). The GitHub server inherits the
    environment unchanged: that is how `GITHUB_TOKEN` reaches it and only it.
    """
    from coder_agent.sandbox import env_overrides

    config = {
        "coder-tools": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "coder_agent.mcp_server.server", str(repo.resolve())],
            "env": {**os.environ, **env_overrides()},
        }
    }
    if github:
        config["github"] = {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "coder_agent.mcp_server.github"],
            "env": dict(os.environ),
        }
    return config


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


async def load_tools(repo: Path, *, github: bool = False) -> list[BaseTool]:
    """Discover and return the tools for `repo`, plus the GitHub read tools when asked.

    `get_tools` returns one flat list across servers; the model never learns which process
    answers which tool, and `ToolNode` does not care either.
    """
    client = MultiServerMCPClient(server_config(repo, github=github))
    tools = await client.get_tools()
    return [strict_arguments(t) for t in tools if t.name not in GITHUB_WRITE_TOOLS]
