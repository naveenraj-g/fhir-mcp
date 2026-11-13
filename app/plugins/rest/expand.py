"""
Expanding a generic FHIR OpenAPI spec into per-resource operations.

Some FHIR servers describe their REST API once, generically, rather than per
resource type. Medplum's /openapi.json has six paths in total:

    GET    /fhir/R4/{resourceType}          search
    POST   /fhir/R4/{resourceType}          createResource   (body: ResourceList)
    GET    /fhir/R4/{resourceType}/{id}     readResource
    ...

Fed to FastMCP as-is, that yields one `search` tool for all ~150 resource
types, a free-text `resourceType` argument, and a request body typed as the
union of every resource — a poor tool for an LLM, and impossible to limit to
"Organization and Location" with path rules.

`expand_resource_types()` rewrites the spec before FastMCP sees it: each
templated path is copied once per configured type with `{resourceType}`
substituted, the `resourceType` parameter dropped, the operationId made
type-specific (`readResource` → `read_organization`, `search` →
`search_organization`), and every `ResourceList` reference narrowed to that
type's own schema. The result looks exactly like a per-resource spec, so the
normal include/exclude rules apply to it.
"""

import copy
import re
from typing import Any

_PLACEHOLDER = "{resourceType}"
_RESOURCE_UNION = "#/components/schemas/ResourceList"
_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _is_resource_type_param(param: dict[str, Any], spec: dict[str, Any]) -> bool:
    if "$ref" in param:
        name = param["$ref"].rsplit("/", 1)[-1]
        param = spec.get("components", {}).get("parameters", {}).get(name, {})
    return param.get("in") == "path" and param.get("name") == "resourceType"


def _narrow(node: Any, resource_type: str) -> Any:
    """Replace every ResourceList $ref with the concrete type's schema."""
    if isinstance(node, dict):
        if node.get("$ref") == _RESOURCE_UNION:
            return {**node, "$ref": f"#/components/schemas/{resource_type}"}
        return {k: _narrow(v, resource_type) for k, v in node.items()}
    if isinstance(node, list):
        return [_narrow(v, resource_type) for v in node]
    return node


def _operation_id(operation_id: str, resource_type: str) -> str:
    if "Resource" in operation_id:
        return _snake(operation_id.replace("Resource", resource_type, 1))
    return f"{_snake(operation_id)}_{_snake(resource_type)}"


def expand_resource_types(spec: dict[str, Any], resource_types: list[str]) -> dict[str, Any]:
    if not resource_types:
        return spec
    schemas = spec.get("components", {}).get("schemas", {})
    unknown = [t for t in resource_types if schemas and t not in schemas]
    if unknown:
        raise ValueError(f"resource_types not defined in the spec's components.schemas: {unknown}")

    out = copy.deepcopy(spec)
    paths: dict[str, Any] = {}
    for path, item in spec.get("paths", {}).items():
        if _PLACEHOLDER not in path:
            paths[path] = item
            continue
        for resource_type in resource_types:
            new_item: dict[str, Any] = {}
            for key, value in item.items():
                if key == "parameters":  # path-level parameters
                    new_item[key] = [p for p in value if not _is_resource_type_param(p, spec)]
                    continue
                if key not in _METHODS:
                    new_item[key] = copy.deepcopy(value)
                    continue
                op = _narrow(copy.deepcopy(value), resource_type)
                op["parameters"] = [p for p in op.get("parameters", []) if not _is_resource_type_param(p, spec)]
                if op.get("operationId"):
                    op["operationId"] = _operation_id(op["operationId"], resource_type)
                op["tags"] = [resource_type]
                if op.get("summary"):
                    op["summary"] = f"{op['summary']} ({resource_type})"
                new_item[key] = op
            paths[path.replace(_PLACEHOLDER, resource_type)] = new_item
    out["paths"] = paths
    # Medplum's resource schemas omit `"type": "object"`; without it FastMCP
    # can't tell a response is an object and wraps every result in
    # {"result": ...}. A FHIR resource is always a JSON object.
    out_schemas = out.get("components", {}).get("schemas", {})
    for name in [*resource_types, "Bundle", "OperationOutcome"]:
        if name in out_schemas:
            out_schemas[name].setdefault("type", "object")
    return out
