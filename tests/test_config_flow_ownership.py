"""Config flows must not open an unowned Modbus session."""
from __future__ import annotations

import ast
from pathlib import Path


def test_config_flow_never_constructs_modbus_client():
    source = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "unite_ev_charger"
        / "config_flow.py"
    ).read_text()
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "WebastoModbus"
    ]

    assert calls == []
