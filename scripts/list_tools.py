"""Print every tool the MCP servers advertise, with its argument schema.

This is what an MCP client sees after the handshake: exactly the names, descriptions and JSON
schemas the model will be prompted with. Reading it is the fastest way to review the tool
descriptions as prompt text. `--github` also starts the GitHub server and shows the merged list
the model gets during `coder fix-issue` (the write tool is filtered out by the client).

Run:  uv run python scripts/list_tools.py [repo_path] [--github]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from coder_agent.tools.client import load_tools


async def main(repo: Path, github: bool) -> None:
    tools = await load_tools(repo, github=github)
    servers = "coder-tools + github" if github else "coder-tools"
    print(f"{len(tools)} tools from {servers} for {repo.resolve()}\n")
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
    args = [a for a in sys.argv[1:] if a != "--github"]
    asyncio.run(main(Path(args[0] if args else "."), github="--github" in sys.argv))
