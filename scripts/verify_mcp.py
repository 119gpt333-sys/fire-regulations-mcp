from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify() -> None:
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=str(root / ".venv" / "bin" / "python"),
        args=["-m", "fire_mcp.server"],
        cwd=root,
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        resources = await session.list_resources()
        prompts = await session.list_prompts()
        expected = {
            "search_current_rules",
            "get_rule_as_of",
            "trace_exception_path",
            "get_source_status",
            "list_pending_changes",
        }
        actual = {tool.name for tool in tools.tools}
        if not expected.issubset(actual):
            raise RuntimeError(f"누락 도구: {sorted(expected - actual)}")
        result = await session.call_tool(
            "search_current_rules",
            {
                "query": os.getenv("FIRE_MCP_VERIFY_QUERY", "가스누설경보기"),
                "as_of": os.getenv("FIRE_MCP_VERIFY_AS_OF", "2026-07-25"),
                "limit": 2,
            },
        )
        if result.isError or not result.content:
            raise RuntimeError("검색 도구 호출 실패")
        rendered = "\n".join(
            item.text for item in result.content if getattr(item, "type", None) == "text"
        )
        if "OC=" in rendered:
            raise RuntimeError("MCP 결과의 공식 URL에 API 인증값이 노출되었습니다.")
        if '"effective_to"' not in rendered or '"review_status"' not in rendered:
            raise RuntimeError("시간구간 또는 승인상태 근거가 MCP 결과에서 누락되었습니다.")
        print(
            f"tools={len(actual)} resources={len(resources.resources)} "
            f"prompts={len(prompts.prompts)}"
        )
        print("search_current_rules=ok")


if __name__ == "__main__":
    asyncio.run(verify())
