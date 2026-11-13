"""
Live smoke test: drives a running fhir-mcp over Streamable HTTP like a real
MCP client — list tools, then create → read → search → patch → delete one
Organization — and prints one line per step.

    FHIR_MCP_TOKEN=<IAM JWT> uv run python scripts/live_smoke.py <flavor> [url]

flavor = which tool set the running server exposes:
    internal-rest     connector internal, plugin rest     (→ fhir-server)
    universal         connector universal, plugin graphql (→ fhir-gql → its active connector)
    internal-graphql  connector internal, plugin graphql  (→ fhir-gql → its active connector)
    medplum-rest      connector medplum,  plugin rest     (→ Medplum REST)
    medplum-graphql   connector medplum,  plugin graphql  (→ Medplum $graphql)

The token is read from the environment and never printed. The created
Organization is deleted at the end (also on failure, when possible).
"""

import asyncio
import json
import os
import sys
import time
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

NAME = f"MCP Smoke {int(time.time())}"
# Valid against the deployment's India Organization profile (country: IN on
# fhir-server): GSTIN + PAN identifiers, type, telecom and address required.
ORG = {
    "resourceType": "Organization",
    "name": NAME,
    "active": True,
    "alias": ["Smoke Alias"],
    "identifier": [
        {"use": "official", "system": "https://fhir-server.dev/fhir/identifier-system/gstin",
         "value": "29AAFCS1234N1Z5"},
        {"use": "official", "system": "https://fhir-server.dev/fhir/identifier-system/pan",
         "value": "AAFCS1234N"},
    ],
    "type": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/organization-type",
                          "code": "prov", "display": "Healthcare Provider"}]}],
    "telecom": [{"system": "phone", "value": "+91-80-45678901", "use": "work"}],
    "address": [{"use": "work", "line": ["No. 22, MG Road"], "city": "Bengaluru",
                 "state": "Karnataka", "postalCode": "560001", "country": "IN"}],
}


def _id(result: Any) -> str:
    data = result.structured_content or {}
    data = data.get("result", data)
    return str(data["id"])


def _brief(result: Any) -> str:
    data = result.structured_content
    return json.dumps(data, ensure_ascii=False)[:160] if data is not None else str(result.content)[:160]


# Per flavor: (tool, arguments) for each step. `{id}` is filled after create.
FLAVORS: dict[str, dict[str, Any]] = {
    "internal-rest": {
        "create": ("create_organization", ORG),
        "read": ("get_organization_by_id", {"organization_id": "{id}"}),
        "search": ("search_organizations", {"name__exact": NAME}),
        "patch": ("patch_organization", {"organization_id": "{id}", "active": False}),
        "delete": ("delete_organization", {"organization_id": "{id}"}),
    },
    "internal-graphql": {
        "create": ("create_fhir_organization", {"input": {k: v for k, v in ORG.items() if k != "resourceType"}}),
        "read": ("get_fhir_organization", {"id": "{id}"}),
        "search": ("search_fhir_organizations", {"name": NAME}),
        "patch": ("update_fhir_organization", {"id": "{id}", "input": {"active": False}}),
        "delete": ("delete_fhir_organization", {"id": "{id}"}),
    },
    # fhir-gql's tools are the same whichever connector exposes them.
    "universal": None,
    "medplum-rest": {
        "create": ("create_organization", ORG),
        "read": ("read_organization", {"id": "{id}"}),
        "search": ("search_organization", {"criteria": {"name": NAME}}),
        "patch": ("patch_organization",
                  {"id": "{id}", "body": [{"op": "replace", "path": "/active", "value": False}]}),
        "delete": ("delete_organization", {"id": "{id}"}),
    },
    "medplum-graphql": {
        "create": ("organization_create", {"res": ORG}),
        "read": ("organization", {"id": "{id}"}),
        "search": ("organization_list", {"name": NAME}),
        "patch": ("organization_update", {"id": "{id}", "res": {**ORG, "id": "{id}", "active": False}}),
        "delete": ("organization_delete", {"id": "{id}"}),
    },
}


def _fill(value: Any, org_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("{id}", org_id)
    if isinstance(value, dict):
        return {k: _fill(v, org_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, org_id) for v in value]
    return value


async def main(flavor: str, url: str) -> int:
    token = os.environ.get("FHIR_MCP_TOKEN")
    if not token:
        print("set FHIR_MCP_TOKEN to an IAM JWT")
        return 2
    steps = FLAVORS[flavor] or FLAVORS["internal-graphql"]
    failures = 0
    async with Client(StreamableHttpTransport(url, auth=token)) as client:
        tools = sorted(t.name for t in await client.list_tools())
        print(f"[{flavor}] tools ({len(tools)}): {', '.join(tools)}")
        org_id: str | None = None
        for step in ("create", "read", "search", "patch", "delete"):
            if step != "create" and org_id is None:
                print(f"  {step:<7} SKIP (nothing created)")
                continue
            tool, args = steps[step]
            try:
                result = await client.call_tool(tool, _fill(args, org_id or ""))
                if step == "create":
                    org_id = _id(result)
                print(f"  {step:<7} OK   {tool} {_brief(result)}")
                if step == "delete":
                    org_id = None
            except Exception as e:  # report and keep going — we want every step's outcome
                failures += 1
                print(f"  {step:<7} FAIL {tool}: {str(e)[:400]}")
        if org_id is not None:  # clean up after a failed delete step
            tool, args = steps["delete"]
            try:
                await client.call_tool(tool, _fill(args, org_id))
                print(f"  cleanup OK   deleted {org_id}")
            except Exception as e:
                print(f"  cleanup FAIL {org_id}: {str(e)[:200]}")
    return 1 if failures else 0


if __name__ == "__main__":
    flavor = sys.argv[1] if len(sys.argv) > 1 else "internal-rest"
    url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8010/mcp"
    sys.exit(asyncio.run(main(flavor, url)))
