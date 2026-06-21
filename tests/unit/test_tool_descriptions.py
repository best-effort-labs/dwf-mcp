from __future__ import annotations

from dwf_mcp import cookbook
from dwf_mcp.server import build_app
from dwf_mcp.tool_descriptions import TOOL_DESCRIPTIONS


def _tool_names() -> set[str]:
    return set(build_app(backend_name="fake")._tools)


def test_every_tool_has_a_description_and_no_extras():
    assert set(TOOL_DESCRIPTIONS) == _tool_names()


def test_no_description_is_blank():
    for name, desc in TOOL_DESCRIPTIONS.items():
        assert desc and desc.strip(), f"{name} has a blank description"
        assert "TODO" not in desc and "TBD" not in desc, f"{name} has a placeholder"


def test_recipe_tool_references_exist():
    referenced = cookbook.recipe_tool_names()
    unknown = referenced - _tool_names()
    assert not unknown, f"cookbook references nonexistent tools: {sorted(unknown)}"
