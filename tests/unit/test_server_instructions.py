from __future__ import annotations

from dwf_mcp.server import build_app, build_server


def test_server_has_cookbook_instructions():
    app = build_app(backend_name="fake")
    server = build_server(app)
    opts = server.create_initialization_options()
    instr = opts.instructions or ""
    assert "cookbook" in instr.lower()
    assert "dwf://cookbook/index" in instr
