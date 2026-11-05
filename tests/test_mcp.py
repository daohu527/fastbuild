import asyncio
import sys

import cadquery as cq
import pytest

pytest.importorskip(
    "cadquery_mcp_server", reason="Install .[mcp] for upstream MCP smoke"
)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def test_upstream_mcp_can_inspect_and_export_step(demo_config, tmp_path):
    destination = tmp_path / "mcp.step"

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-u", "-c", "from cadquery_mcp_server import run; run()"],
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {"inspect", "render", "export"} <= {
                    tool.name for tool in tools.tools
                }
                code = (
                    "from pathlib import Path\n"
                    "from fastbuild.config import load_design\n"
                    "from fastbuild.pipeline import create_model\n"
                    f"result = create_model(load_design(Path({str(demo_config)!r})))"
                    ".assembly"
                )
                inspected = await session.call_tool("inspect", {"code": code})
                assert not inspected.isError
                assert "Solids: 3" in inspected.content[0].text
                rendered = await session.call_tool(
                    "render", {"code": code, "image_format": "svg"}
                )
                assert not rendered.isError
                exported = await session.call_tool(
                    "export", {"code": code, "filename": str(destination)}
                )
                assert not exported.isError
                assert destination.is_file()
                shape = cq.importers.importStep(str(destination)).val()
                assert shape.isValid()
                assert len(shape.Solids()) == 3
                invalid = await session.call_tool(
                    "inspect", {"code": "raise ValueError('bad design')"}
                )
                assert invalid.isError

    asyncio.run(asyncio.wait_for(run(), timeout=60))
