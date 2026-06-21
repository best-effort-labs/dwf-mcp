# tests/unit/test_cookbook_resources.py
from __future__ import annotations

import asyncio

import pytest

from dwf_mcp import cookbook
from dwf_mcp.server import build_app, build_server, cookbook_resource_handlers


def test_list_resources_returns_five_cookbook_uris():
    app = build_app(backend_name="fake")
    build_server(app)  # registers handlers (smoke: must not raise)
    lst, _read = cookbook_resource_handlers()
    resources = asyncio.run(lst())
    uris = {str(r.uri) for r in resources}
    assert uris == {f"dwf://cookbook/{n}" for n in cookbook.doc_names()}


def test_read_resource_returns_markdown():
    _lst, read = cookbook_resource_handlers()
    text = asyncio.run(read("dwf://cookbook/index"))
    assert text.lstrip().startswith("#")


def test_read_unknown_resource_raises():
    _lst, read = cookbook_resource_handlers()
    with pytest.raises((ValueError, KeyError)):
        asyncio.run(read("dwf://cookbook/nope"))


def test_read_non_cookbook_uri_raises_valueerror():
    _lst, read = cookbook_resource_handlers()
    with pytest.raises(ValueError):
        asyncio.run(read("file:///etc/passwd"))
