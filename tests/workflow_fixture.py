"""Shared workflow fixture for custom-strategy tests.

A custom strategy only executes from a confirmed visual workflow, so every test
that drives CustomAiStrategy or the custom runtime prompt must supply one even
when the AI client is faked.
"""

from __future__ import annotations

from typing import Any


def stage(name: str, action: str) -> dict[str, Any]:
    """Return one entry -> action stage."""
    return {
        "entry_node_id": f"{name}_entry",
        "nodes": [
            {"id": f"{name}_entry", "type": "entry", "stage": name, "label": "入口"},
            {"id": f"{name}_action", "type": "action", "label": action, "action": {"kind": action}},
        ],
        "edges": [
            {
                "id": f"{name}_edge",
                "source": f"{name}_entry",
                "target": f"{name}_action",
                "source_handle": "next",
            },
        ],
    }


def minimal_workflow() -> dict[str, Any]:
    """Return a fresh, minimal, compilable workflow (entry -> action per stage)."""
    return {
        "schema_version": 1,
        "open": stage("open", "no_action"),
        "position": stage("position", "hold"),
    }


def workflow_with_ema() -> dict[str, Any]:
    """Return a workflow whose open stage requires EMA10/EMA20 indicators."""

    def indicator(alias: str, length: int) -> dict[str, Any]:
        return {
            "kind": "indicator",
            "indicator": "ema",
            "alias": alias,
            "source": "close",
            "params": {"length": length},
        }

    return {
        "schema_version": 1,
        "open": {
            "entry_node_id": "open_entry",
            "nodes": [
                {"id": "open_entry", "type": "entry", "stage": "open", "label": "入口"},
                {
                    "id": "open_cond",
                    "type": "condition",
                    "label": "EMA10 在 EMA20 上方",
                    "condition": {
                        "kind": "comparison",
                        "description": "EMA10 above EMA20",
                        "left": indicator("ema10", 10),
                        "operator": "gt",
                        "right": indicator("ema20", 20),
                    },
                },
                {
                    "id": "open_action",
                    "type": "action",
                    "label": "开多",
                    "action": {"kind": "open_buy", "entry_mode": "market"},
                },
            ],
            "edges": [
                {"id": "e1", "source": "open_entry", "target": "open_cond", "source_handle": "next"},
                {"id": "e2", "source": "open_cond", "target": "open_action", "source_handle": "yes"},
                {"id": "e3", "source": "open_cond", "target": "open_action", "source_handle": "no"},
            ],
        },
        "position": stage("position", "hold"),
    }
