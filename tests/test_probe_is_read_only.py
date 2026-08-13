"""The probe runs against production tenants, so its read-only property is
enforced here rather than left to reviewer memory."""

from __future__ import annotations

import ast
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1] / "scripts" / "st_probe.py"

WRITE_METHODS = {"create_cost", "update_cost"}


def _referenced_names(source: str) -> set[str]:
    """Names the code actually touches. Parsed rather than grepped, so the
    module docstring may name the write methods to explain their absence."""
    tree = ast.parse(source)
    return {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def test_probe_calls_no_write_methods() -> None:
    offenders = WRITE_METHODS & _referenced_names(PROBE.read_text())
    assert not offenders, (
        f"scripts/st_probe.py calls {', '.join(sorted(offenders))}. The probe is "
        "read-only; writes belong in the app, behind the preview gate."
    )


def test_guard_would_catch_a_write_call() -> None:
    """The guard is only worth having if it fails on the thing it forbids."""
    assert WRITE_METHODS & _referenced_names("client.update_cost(1, x)")
