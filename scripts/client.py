"""Minimal MCP stdio client for local acceptance checks.

Usage:
    python scripts/client.py list
    python scripts/client.py call geocode_place '{"name": "聊城大学"}'
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def run(action: str, tool_name: str | None, tool_args: dict) -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "main.py")],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            if action == "list":
                print(f"server: {init.serverInfo.name} {init.serverInfo.version}")
                result = await session.list_tools()
                for tool in result.tools:
                    print(
                        json.dumps(
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "inputSchema": tool.inputSchema,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                return 0

            assert tool_name is not None
            result = await session.call_tool(tool_name, tool_args)
            payload = result.structuredContent
            if payload is not None:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for item in result.content:
                    text = getattr(item, "text", None)
                    print(text if text is not None else item)
            return 1 if result.isError else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["list", "call"])
    parser.add_argument("tool_name", nargs="?")
    parser.add_argument("tool_args", nargs="?", default="{}")
    ns = parser.parse_args()
    if ns.action == "call" and not ns.tool_name:
        parser.error("call requires a tool name")
    try:
        args = json.loads(ns.tool_args)
    except json.JSONDecodeError as exc:
        parser.error(f"tool args must be JSON: {exc}")
    if not isinstance(args, dict):
        parser.error("tool args must be a JSON object")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(run(ns.action, ns.tool_name, args))


if __name__ == "__main__":
    raise SystemExit(main())