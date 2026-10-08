"""The MCP server: :class:`~local_data_platform.mcp_server.tools.LakeTools` over the ``mcp`` SDK.

The server is built on the SDK's low-level ``mcp.server.lowlevel.Server`` (mcp 2.x), not on
``MCPServer`` (the renamed FastMCP): ``MCPServer`` calls ``logging.basicConfig`` when it is
constructed, which a library must not do, and the low-level server lets the tool schemas
carry this server's row cap. Tool calls run in a worker thread, so a long query never
blocks the event loop; the sandbox serializes them.

Each tool result carries the JSON both as ``structuredContent`` and as one text block.
A failed call comes back with ``isError: true`` and ``{"error": {"type", "message"}}``.
"""

from __future__ import annotations

import json
from typing import Any

from local_data_platform import __version__
from local_data_platform.exceptions import EngineNotFound
from local_data_platform.logger import get_logger

from .tools import LakeTools, ToolOutcome

logger = get_logger(__name__)

SERVER_NAME = "local-data-platform"
MCP_INSTALL_HINT = 'pip install "local-data-platform[mcp]"'


def import_mcp() -> tuple[Any, Any]:
    """Import the ``mcp`` SDK, returning ``(mcp_types, lowlevel Server class)``.

    Raises:
        EngineNotFound: If the ``mcp`` package (version 2 or later) is not installed.
    """
    try:
        import mcp_types
        from mcp.server.lowlevel import Server
    except ImportError as error:
        raise EngineNotFound(f"The MCP server needs the mcp package (version 2 or later). Install it with: "
                             f"{MCP_INSTALL_HINT}") from error
    return mcp_types, Server


def to_call_tool_result(outcome: ToolOutcome, types: Any = None) -> Any:
    """Turn a :class:`ToolOutcome` into an ``mcp_types.CallToolResult``."""
    if types is None:
        types, _ = import_mcp()
    text = json.dumps(outcome.data, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                structured_content=outcome.data, is_error=not outcome.ok)


def build_server(tools: LakeTools, *, name: str = SERVER_NAME) -> Any:
    """Build a low-level MCP ``Server`` that serves ``tools``.

    Args:
        tools: The tools to expose. The caller keeps ownership and closes them.
        name: The server name reported to clients.

    Returns:
        An ``mcp.server.lowlevel.Server``. Run it over stdio with :func:`serve_stdio`, or
        connect to it in-process with ``mcp.Client(server)`` in tests.

    Raises:
        EngineNotFound: If the ``mcp`` SDK is not installed.
    """
    types, Server = import_mcp()
    import anyio

    annotations = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                        open_world_hint=False)
    listed = [types.Tool(name=spec.name, title=spec.title, description=spec.description,
                         input_schema=spec.input_schema, annotations=annotations)
              for spec in tools.tool_specs()]

    async def list_tools(ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=listed)

    async def call_tool(ctx: Any, params: Any) -> Any:
        arguments = dict(params.arguments or {})
        outcome = await anyio.to_thread.run_sync(tools.call, params.name, arguments)
        return to_call_tool_result(outcome, types)

    return Server(name, version=__version__, title="Local Data Platform (read-only)",
                  instructions=tools.instructions(), on_list_tools=list_tools, on_call_tool=call_tool)


async def serve_stdio_async(tools: LakeTools) -> None:
    """Serve ``tools`` over this process's stdin and stdout until the client disconnects."""
    import_mcp()
    from mcp.server.stdio import stdio_server

    server = build_server(tools)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_stdio(tools: LakeTools) -> None:
    """Blocking wrapper around :func:`serve_stdio_async`."""
    import anyio

    anyio.run(serve_stdio_async, tools)


__all__ = ["MCP_INSTALL_HINT", "SERVER_NAME", "build_server", "import_mcp", "serve_stdio", "serve_stdio_async",
           "to_call_tool_result"]
