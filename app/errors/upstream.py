"""
Turning backend failures into errors the MCP client can act on.

FastMCP catches whatever a tool raises and, with `mask_error_details=True`
(app.main), replaces anything that isn't a `ToolError` with a bare "Error
calling tool 'x'". That's right for bugs, but wrong for a backend that
answered 422 with an OperationOutcome explaining exactly which element is
invalid — the LLM needs that text to fix its call. Middleware can't do this
mapping: the masking happens inside the tool layer, below the middleware
chain.

So every connector tool is wrapped (a FastMCP `Transform`, applied to the
connector's provider in app.connectors) and the backend failure becomes a
`ToolError` *inside* the tool:

  non-2xx response  → "HTTP error 422: Unprocessable Entity - {OperationOutcome}"
                      (FastMCP's own message: status + bounded body)
  transport failure → "Backend unreachable (ConnectError): ..."
"""

from collections.abc import Sequence
from typing import Any

import httpx2
from fastmcp.exceptions import ToolError
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.tool_transform import forward_raw
from fastmcp.utilities.versions import VersionSpec


async def _surface_upstream_errors(**kwargs: Any) -> ToolResult:
    try:
        return await forward_raw(**kwargs)
    except httpx2.HTTPStatusError as e:
        raise ToolError(str(e)) from e
    except httpx2.TransportError as e:
        raise ToolError(f"Backend unreachable ({type(e).__name__}): {e}") from e


def _wrap(tool: Tool) -> Tool:
    return Tool.from_tool(tool, transform_fn=_surface_upstream_errors)


class UpstreamErrorTransform(Transform):
    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [_wrap(t) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return _wrap(tool) if tool is not None else None
