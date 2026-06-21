from __future__ import annotations

from dwf_mcp.server import build_app, build_server


def test_build_server_registers_all_tools():
    app = build_app(backend_name="fake")
    server = build_server(app)
    assert server.name == "dwf-mcp"
    assert app._tools, "app should have tools registered"
