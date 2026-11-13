"""
Turning an introspected GraphQL schema into what a tool needs:

- `input_schema()` — a field's arguments as a JSON Schema (the tool's input),
  with input objects as `$defs` so recursive types stay finite;
- `selection_set()` — the field's return type as a GraphQL selection set,
  walked to a fixed depth (GraphQL has no "select everything"), over the
  type's own data fields only (no reverse-reference searches, no large
  unions — see `_is_reverse_query`);
- `type_ref()` — an argument's type as GraphQL source (`[String!]!`), for the
  operation's variable declarations.
"""

from typing import Any

TypeRef = dict[str, Any]

# Unions larger than this are selected as `__typename` only.
_MAX_UNION_MEMBERS = 10

# Built-in and fhir-gql/strawberry scalars. An unknown custom scalar becomes
# an unconstrained schema — the backend still validates it.
_SCALARS: dict[str, dict[str, Any]] = {
    "String": {"type": "string"},
    "ID": {"type": "string"},
    "Int": {"type": "integer"},
    "Float": {"type": "number"},
    "Boolean": {"type": "boolean"},
    # strawberry's Decimal accepts a number or a numeric string; a string keeps
    # FHIR decimal precision intact.
    "Decimal": {"type": ["number", "string"]},
    "Date": {"type": "string", "format": "date"},
    "DateTime": {"type": "string", "format": "date-time"},
    "Time": {"type": "string", "format": "time"},
    "UUID": {"type": "string", "format": "uuid"},
    "JSON": {},
}


def named_type(ref: TypeRef) -> str:
    while ref.get("ofType") is not None and ref["kind"] in ("NON_NULL", "LIST"):
        ref = ref["ofType"]
    return ref["name"]


def type_ref(ref: TypeRef) -> str:
    match ref["kind"]:
        case "NON_NULL":
            return type_ref(ref["ofType"]) + "!"
        case "LIST":
            return f"[{type_ref(ref['ofType'])}]"
    return ref["name"]


def default_value(arg: dict[str, Any]) -> str | None:
    """Introspection reports a default as GraphQL source text; "null" means
    the same as no default."""
    value = arg.get("defaultValue")
    return None if value in (None, "null") else value


def is_required(arg: dict[str, Any]) -> bool:
    """Non-null with no default — the caller must supply it."""
    return arg["type"]["kind"] == "NON_NULL" and default_value(arg) is None


class SchemaIndex:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.types: dict[str, dict[str, Any]] = {t["name"]: t for t in schema["types"]}
        self.query_type = (schema.get("queryType") or {}).get("name")
        self.mutation_type = (schema.get("mutationType") or {}).get("name")

    def root_fields(self, root: str | None) -> list[dict[str, Any]]:
        if root is None or root not in self.types:
            return []
        return self.types[root].get("fields") or []

    # ── input side ────────────────────────────────────────────────────────

    def input_schema(self, args: list[dict[str, Any]]) -> dict[str, Any]:
        defs: dict[str, Any] = {}
        properties = {a["name"]: self._arg_schema(a, defs) for a in args}
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        required = [a["name"] for a in args if is_required(a)]
        if required:
            schema["required"] = required
        if defs:
            schema["$defs"] = defs
        return schema

    def _arg_schema(self, arg: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
        schema = dict(self._ref_schema(arg["type"], defs))
        if arg.get("description"):
            schema["description"] = arg["description"]
        if (default := default_value(arg)) is not None:
            schema["description"] = f"{schema.get('description', '')} Default: {default}.".strip()
        return schema

    def _ref_schema(self, ref: TypeRef, defs: dict[str, Any]) -> dict[str, Any]:
        match ref["kind"]:
            case "NON_NULL":
                return self._ref_schema(ref["ofType"], defs)
            case "LIST":
                return {"type": "array", "items": self._ref_schema(ref["ofType"], defs)}
            case "SCALAR":
                return _SCALARS.get(ref["name"], {})
            case "ENUM":
                values = [v["name"] for v in self.types[ref["name"]].get("enumValues") or []]
                return {"type": "string", "enum": values}
            case "INPUT_OBJECT":
                name = ref["name"]
                if name not in defs:
                    defs[name] = {}  # placeholder first, so recursion terminates
                    defs[name] = self._input_object_schema(name, defs)
                return {"$ref": f"#/$defs/{name}"}
        return {}

    def _input_object_schema(self, name: str, defs: dict[str, Any]) -> dict[str, Any]:
        t = self.types[name]
        fields = t.get("inputFields") or []
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {f["name"]: self._arg_schema(f, defs) for f in fields},
        }
        if t.get("description"):
            schema["description"] = t["description"]
        required = [f["name"] for f in fields if is_required(f)]
        if required:
            schema["required"] = required
        return schema

    # ── output side ───────────────────────────────────────────────────────

    def selection_set(self, ref: TypeRef, depth: int) -> str:
        """Selection for a field returning `ref`; "" when it's a leaf."""
        return self._select(named_type(ref), depth, frozenset())

    def _is_resource_or_connection(self, name: str) -> bool:
        t = self.types[name]
        if t["kind"] != "OBJECT":
            return False
        fields = {f["name"] for f in t.get("fields") or []}
        return "resourceType" in fields or "edges" in fields or name.endswith("Connection")

    def _is_reverse_query(self, field: dict[str, Any], sub: str) -> bool:
        """A field that takes arguments and returns resources is a search,
        not data. Medplum gives every resource ~300 of them
        (`AccountList(...)`, `AppointmentConnection(...)` — everything that
        references it); selecting them would pull in the whole graph.

        Arguments alone don't decide it: FHIR GraphQL list *elements*
        (`identifier`, `telecom`) take optional filter arguments too, and
        fhir-gql's search result holds its resources in a plain,
        argument-less `organizations` field."""
        return bool(field.get("args")) and self._is_resource_or_connection(sub)

    def _select(self, name: str, depth: int, seen: frozenset[str]) -> str:
        t = self.types[name]
        kind = t["kind"]
        if kind in ("SCALAR", "ENUM"):
            return ""
        if kind == "UNION":
            parts = ["__typename"]
            members = t.get("possibleTypes") or []
            if len(members) > _MAX_UNION_MEMBERS:
                # e.g. Medplum's ResourceList (every resource type) — expanding
                # it would select ~150 full resources; the type name is enough.
                return "{ __typename }"
            for member in members:
                inner = self._select(member["name"], depth, seen)
                if inner:
                    parts.append(f"... on {member['name']} {inner}")
            return "{ " + " ".join(parts) + " }"

        seen = seen | {name}
        parts: list[str] = []
        for field in t.get("fields") or []:
            # A field that needs arguments can't be selected without them.
            if any(is_required(a) for a in field.get("args") or []):
                continue
            sub = named_type(field["type"])
            if self._is_reverse_query(field, sub):
                continue
            if self.types[sub]["kind"] in ("SCALAR", "ENUM"):
                parts.append(field["name"])
            elif depth > 1 and sub not in seen:
                inner = self._select(sub, depth - 1, seen)
                if inner:
                    parts.append(f"{field['name']} {inner}")
        return "{ " + " ".join(parts or ["__typename"]) + " }"
