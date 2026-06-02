# dwf-mcp

MCP server exposing the Digilent WaveForms SDK.

See `docs/plans/2026-06-02-dwf-mcp-design.md` for design.

## Status

Foundation complete (stage 1 of N):
- Safety policy, pin allocator, artifact writer
- DwfBackend ABC + fake + pydwf backends
- DwfDevice session with lazy open / idle timeout / unplug recovery
- MCP server with `waveforms.open`/`close`/`status`/`list_pins`
- AD3 pin map (provisional — confirm against reference manual before stage 2)

Stage 2: scope + supply + i2c vertical slice.
Stage 3: remaining instruments (logic, awg, pattern, dio, dmm, can, spi, uart).
Stage 4: passive decoders.
