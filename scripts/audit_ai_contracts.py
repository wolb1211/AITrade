"""Cross-check what we ask the AI for against what we actually read back.

Three things have to agree for every endpoint:

1. the shape the prompt demands (the schema text injected into the system
   prompt, plus any per-call override);
2. the keys the response parser insists on before it accepts the answer;
3. the keys the strategies read out of the parsed answer.

They drifted three times in production, each time silently: a strategy read a
name the prompt never asked for, so it fell back to defaults with nothing in the
logs, or the parser demanded a name the prompt did ask for and rejected the
answer as malformed. This script lists the three sets per endpoint and prints
what does not line up.

Run it from the repository root:

    python scripts/audit_ai_contracts.py

Known limitation: reads are attributed through the client method that produced
the reply plus one level of helper, so when a single function calls two
different endpoints - PA's evaluate_open asks the diagnosis endpoint and then
the open endpoint - the diagnosis line also lists the open decision's keys.
Verified by hand: PA's diagnosis parsing reads none of them, so that line is
noise from the attribution, not drift in the code.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ai_service  # noqa: E402
from app.services.ai_service import (  # noqa: E402
    _PA_OPEN_SCHEMA,
    _REQUIRED_VERDICT_KEYS,
    _TURTLE_OPEN_RISK_SCHEMA,
    _TURTLE_POSITION_REVIEW_SCHEMA,
    _json_api_system_prompt,
)

# Call sites that talk to a model, with the file that reads the answer.
_CONSUMERS = (
    ROOT / "app" / "strategies" / "turtle_agent.py",
    ROOT / "app" / "strategies" / "pa_agent_lite.py",
    ROOT / "app" / "strategies" / "custom_ai.py",
)

_ENDPOINTS = ("open", "position", "pa_diag", "workflow_stage")


def _declared_shapes() -> dict[tuple[str, str], frozenset[str]]:
    """Keys the model is told to answer with, per strategy file and endpoint.

    A call site may declare its own shape, which replaces the generic one for
    that call only, so the audit has to know which file uses which. Taken from
    the same constants the service uses, so this cannot drift from the prompts it
    audits.
    """
    generic_open = _keys_of(_json_api_system_prompt("open", ""))
    generic_position = _keys_of(_json_api_system_prompt("position", ""))
    return {
        ("turtle_agent.py", "open"): generic_open | _keys_of(_TURTLE_OPEN_RISK_SCHEMA),
        ("turtle_agent.py", "position"): generic_position | _keys_of(_TURTLE_POSITION_REVIEW_SCHEMA),
        ("pa_agent_lite.py", "open"): generic_open | _keys_of(_PA_OPEN_SCHEMA),
        ("pa_agent_lite.py", "position"): generic_position,
        ("pa_agent_lite.py", "pa_diag"): _keys_of(_json_api_system_prompt("pa_diag", "")),
        ("custom_ai.py", "open"): generic_open,
        ("custom_ai.py", "position"): generic_position,
    }


def _keys_of(schema_text: str) -> frozenset[str]:
    """Top-level key names from a JSON shape shown to the model."""
    try:
        shape = json.loads(schema_text)
    except (TypeError, ValueError):
        names: set[str] = set()
        for match in re.finditer(r'"([a-z_]+)"\s*:', schema_text):
            names.add(match.group(1))
        return frozenset(names)
    if isinstance(shape, dict):
        return frozenset(str(key) for key in shape)
    return frozenset()


def _content_names(node: ast.AST) -> set[str]:
    """Locals inside this function that hold the model's parsed reply.

    Only these are audited: a function also reads its own config and rule plan,
    and counting those keys would bury the real mismatches in noise.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            value = child.value
            holds_reply = isinstance(value, ast.Attribute) and value.attr == "content"
            if holds_reply:
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names or {"content", "parsed"}


def _string_keys(node: ast.AST, reply_names: set[str]) -> set[str]:
    """String keys read out of the model's reply inside a node."""
    found: set[str] = set()

    def receiver_is_reply(call_or_sub: ast.AST) -> bool:
        target = call_or_sub.value if isinstance(call_or_sub, ast.Subscript) else None
        if isinstance(call_or_sub, ast.Call) and isinstance(call_or_sub.func, ast.Attribute):
            target = call_or_sub.func.value
        if isinstance(target, ast.Name):
            return target.id in reply_names
        if isinstance(target, ast.Attribute):
            return target.attr == "content"
        return False

    for child in ast.walk(node):
        if isinstance(child, ast.Subscript) and receiver_is_reply(child):
            index = child.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                found.add(index.value)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr == "get" and child.args and receiver_is_reply(child):
                first = child.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
    return found


# Client methods the strategies call, mapped to the endpoint each one uses.
# The strategies never pass endpoint= directly, so without this the audit skips
# exactly the functions whose keys have drifted in production.
_METHOD_ENDPOINTS = {
    "turtle_open_risk_decision": "open",
    "turtle_position_review": "position",
    "pa_open_decision": "open",
    "pa_position_decision": "position",
    "pa_open_diagnosis": "pa_diag",
    "custom_open_decision": "open",
    "custom_position_decision": "position",
}


def _call_sites() -> list[dict[str, Any]]:
    """Per strategy file: the endpoints it talks to and the keys it reads back.

    Reads are attributed by the client method that produced the reply, plus the
    module-level helpers that function calls - a strategy often parses the reply
    in a small helper, and without following that call the audit would credit
    the keys to the wrong endpoint or miss them entirely.
    """
    sites: list[dict[str, Any]] = []
    for path in _CONSUMERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        functions = {
            node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }

        def calls_in(node: ast.AST) -> set[str]:
            names: set[str] = set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if isinstance(child.func, ast.Attribute):
                    names.add(child.func.attr)
                elif isinstance(child.func, ast.Name):
                    names.add(child.func.id)
            return names

        per_endpoint: dict[str, set[str]] = {}
        # Which call sites serve which endpoint, so a helper shared by two of
        # them can be excluded instead of crediting its keys to both.
        callers_per_endpoint: dict[str, set[str]] = {}
        own_reads: dict[str, set[str]] = {}
        for name, node in functions.items():
            for called in calls_in(node):
                endpoint = _METHOD_ENDPOINTS.get(called)
                if endpoint is None:
                    continue
                callers_per_endpoint.setdefault(endpoint, set()).add(name)
                own_reads.setdefault(endpoint, set()).update(
                    _string_keys(node, _content_names(node))
                )

        for endpoint, callers in callers_per_endpoint.items():
            keys = set(own_reads.get(endpoint, set()))
            for caller in callers:
                node = functions.get(caller)
                if node is None:
                    continue
                for callee in calls_in(node):
                    helper = functions.get(callee)
                    if helper is None:
                        continue
                    # Only helpers this endpoint alone reaches: a shared one may
                    # serve another endpoint, and its keys would be miscredited.
                    serves_others = any(
                        callee in calls_in(functions[other])
                        for other_endpoint, others in callers_per_endpoint.items()
                        if other_endpoint != endpoint
                        for other in others
                        if other in functions
                    )
                    if serves_others:
                        continue
                    keys |= _string_keys(helper, _content_names(helper))
            per_endpoint[endpoint] = keys

        if per_endpoint:
            sites.append({"file": path.name, "endpoints": per_endpoint})
    return sites


def main() -> int:
    sites = _call_sites()
    declared_shapes = _declared_shapes()
    problems = 0
    for endpoint in _ENDPOINTS:
        required = set(_REQUIRED_VERDICT_KEYS.get(endpoint, ()))
        read: set[str] = set()
        declared: set[str] = set()
        readers: list[tuple[str, set[str]]] = []
        for site in sites:
            keys = site["endpoints"].get(endpoint)
            if keys is None:
                continue
            readers.append((site["file"], keys))
            read |= keys
            declared |= set(declared_shapes.get((site["file"], endpoint), frozenset()))

        print(f"\n=== {endpoint} ===")
        print(f"  prompt asks for : {', '.join(sorted(declared)) or '-'}")
        print(f"  parser requires : {', '.join(sorted(required)) or '(none, any object)'}")
        for name, _keys in readers:
            print(f"  reads in {name}")

        # A reader must only touch names the prompt actually offers, or it will
        # silently read nothing when the model answers as instructed.
        unknown = sorted(key for key in read - declared if not key.startswith("_"))
        if unknown:
            problems += 1
            print(f"  !! read but never asked for: {', '.join(unknown)}")
        if required and not (required & declared):
            problems += 1
            print(f"  !! parser requires {', '.join(sorted(required))}, prompt never offers it")

    print(f"\n{problems} contract problem(s) found")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
