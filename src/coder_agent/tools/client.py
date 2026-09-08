"""MCP client: launches the tool server and exposes its tools as LangChain tools.

`MultiServerMCPClient` can talk to several servers at once (ours, plus later a GitHub or web-search
server). For each server it performs the MCP handshake, calls `tools/list`, and wraps every tool
in a LangChain `BaseTool` whose `args_schema` comes from the server's JSON schema. LangGraph's
`ToolNode` can then call them like any other tool.

Transport is stdio: the client spawns the server as a child process and exchanges JSON-RPC
messages over its stdin/stdout. Nothing touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient


def server_config(repo: Path) -> dict:
    """Connection spec for our own server. Uses the current interpreter so the venv is respected."""
    return {
        "coder-tools": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "coder_agent.mcp_server.server", str(repo.resolve())],
        }
    }


async def load_tools(repo: Path) -> list[BaseTool]:
    """Discover and return the tools for `repo`."""
    client = MultiServerMCPClient(server_config(repo))
    return await client.get_tools()
