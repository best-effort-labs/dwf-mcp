# tests/unit/test_cookbook_loader.py
from __future__ import annotations

import pytest

from dwf_mcp import cookbook


def test_names_are_the_five_docs():
    assert set(cookbook.doc_names()) == {
        "index", "freq-domain", "time-domain", "protocols", "bench"
    }


def test_read_doc_returns_nonempty_markdown():
    for name in cookbook.doc_names():
        text = cookbook.read_doc(name)
        assert text.strip(), f"{name} is empty"
        assert text.lstrip().startswith("#")


def test_read_unknown_doc_raises():
    with pytest.raises(KeyError):
        cookbook.read_doc("does-not-exist")


def test_parse_front_matter_extracts_id_and_tools():
    md = (
        "---\n"
        "id: freq-domain:filter-response\n"
        "tools: [bode.measure, scope.capture]\n"
        "---\n"
        "# A recipe\nbody\n"
    )
    fm, body = cookbook.parse_front_matter(md)
    assert fm["id"] == "freq-domain:filter-response"
    assert fm["tools"] == ["bode.measure", "scope.capture"]
    assert body.strip() == "# A recipe\nbody"


def test_parse_front_matter_absent_returns_empty():
    fm, body = cookbook.parse_front_matter("# no front matter\n")
    assert fm == {}
    assert body.strip() == "# no front matter"


def test_recipe_tool_names_scans_all_docs():
    names = cookbook.recipe_tool_names()
    # Skeletons carry no recipe front matter yet; this also guards against the function
    # crashing/returning None, and will fail loudly (as a reminder) once recipes land.
    assert names == set()
