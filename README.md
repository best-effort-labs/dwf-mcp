# dwf-mcp

MCP server exposing the Digilent WaveForms SDK.

See `docs/plans/2026-06-02-dwf-mcp-design.md` for design.

## Status

Stage 2 complete (3 of N stages):
- Stage 1: safety policy, pin allocator, artifact writer, instrument ABC + registry, DwfDevice with lazy open/idle/unplug recovery, DwfBackend ABC + fake + pydwf, MCP server with `waveforms.open/close/status/list_pins`.
- Stage 2: `scope` (buffer-mode capture), `supply` (safety-gated programmable rails), `i2c` (active master). Centralized `device.gate_output` safety helper; `dwf-safety.log` audit trail; lazy-instantiated instruments with `tools`-map dispatch and exception → result-shape error mapping.

Stage 3: `awg`, `logic`, `pattern`, `dio`, `dmm`, `can`, `spi`, `uart`. Streaming/recording modes. VCD writer. AD3 pin-map verification against the reference manual (load-bearing now that AWG / logic / pattern start to overlap on DIO/clock domains).
Stage 4: passive decoders.
