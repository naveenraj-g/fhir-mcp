"""
Argument-name sanitising for OpenAPI-generated tools.

FHIR search puts modifiers in the parameter *name* — `name:exact`,
`partof:missing`, `type:not-in` — and fhir-server documents each one as its
own query parameter so callers can discover them. FastMCP turns each into a
tool argument with that exact name, but LLM tool-calling APIs only accept
property names matching `^[a-zA-Z0-9_.-]{1,64}$`; a tool with `name:exact`
in its schema is rejected by the client before it is ever called.

The fix is a FastMCP `ToolTransform`: the client sees `name__exact`, and the
transform maps it back to `name:exact` before FastMCP builds the request, so
the backend still receives the real FHIR parameter. `__` stands for `:`
because no FHIR parameter name contains a double underscore, so the mapping
is unambiguous and easy to explain in the server instructions.
"""

import re
from collections.abc import Sequence

from fastmcp.server.transforms import ToolTransform
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransformConfig, ToolTransformConfig

_VALID = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def safe_argument_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", name.replace(":", "__"))
    return safe[:64]


def argument_rename_transform(tools: Sequence[Tool]) -> ToolTransform | None:
    """A ToolTransform renaming every invalid argument name, or None if every
    tool is already fine."""
    configs: dict[str, ToolTransformConfig] = {}
    for tool in tools:
        props = tool.parameters.get("properties", {})
        taken = {n for n in props if _VALID.match(n)}
        renames: dict[str, ArgTransformConfig] = {}
        for name, schema in props.items():
            if _VALID.match(name):
                continue
            new = safe_argument_name(name)
            while new in taken:  # never collide with a real argument
                new = (new[:62] + "_x")[:64]
            taken.add(new)
            description = schema.get("description") or ""
            renames[name] = ArgTransformConfig(
                name=new,
                description=f"FHIR search parameter `{name}`. {description}".strip(),
            )
        if renames:
            configs[tool.name] = ToolTransformConfig(arguments=renames)
    return ToolTransform(configs) if configs else None
