"""Print every tool the MCP server advertises, with its argument schema.

This is what an MCP client sees after the handshake: exactly the names, descriptions and JSON
schemas the model will be prompted with. Reading it is the fastest way to review the tool
descriptions as prompt text.

Run:  uv run python scripts/list_tools.py [repo_path]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from coder_agent.tools.client import load_tools


async def main(repo: Path) -> None:
    tools = await load_tools(repo)
    print(f"{len(tools)} tools from coder-tools for {repo.resolve()}\n")
    for tool in tools:
        print(f"== {tool.name}")
        print(tool.description.strip())
        schema = tool.args_schema
        if isinstance(schema, dict):
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
        else:  # pydantic model
            js = schema.model_json_schema()
            props, required = js.get("properties", {}), set(js.get("required", []))
        for name, spec in props.items():
            mark = "*" if name in required else " "
            print(f"  {mark} {name}: {json.dumps(spec.get('type', spec.get('anyOf', '?')))}")
        print()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
