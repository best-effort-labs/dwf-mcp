# dwf-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes the [Digilent WaveForms SDK](https://digilent.com/reference/test-and-measurement/waveforms/waveforms-sdk/start) for the Analog Discovery 3 (and compatible Digilent devices) to LLM agents. Lets a conversational model drive a real benchtop — scope, AWG, supply, logic analyzer, protocol masters, protocol sniffers, post-process decoders — through a stable tool surface.

## Status

All four stages complete:

- **Stage 1–2:** safety policy, pin allocator, artifact writer, instrument ABC, lazy device + idle/unplug recovery, scope, supply, I2C master.
- **Stage 3:** AWG, logic analyzer (buffer + streaming record), pattern, DIO, DMM, CAN, SPI, UART. VCD writer.
- **Stage 4:** protocol sniff (I2C/UART/CAN spy via protocol engines, SPI sniff via DigitalIn observer), `SpiDecoder` + post-process `decoder.spi` tool.
- **Stage 5:** `I2cDecoder` / `UartDecoder` / `CanDecoder` + post-process `decoder.{i2c,uart,can}` tools + async observe-mode `sniff.{i2c,uart,can,spi}_start/status/stop` tools that use DigitalIn instead of protocol engines (and therefore coexist with active masters on the same wires). 32 MB raw memory cap, 300s session reap, full schema parity between engine-mode and observe-mode artifacts.

338 unit tests, 24 hardware tests passing on AD3 + Jumperless V5.

## Install

```bash
git clone <repo>
cd dwf-mcp
pip install -e ".[vcd,dev]"
```

Requires the WaveForms runtime (which provides `libdwf`) and Python 3.11+. macOS: `brew install --cask digilent-waveforms`. Linux: install WaveForms from Digilent.

## Run

```bash
dwf-mcp
```

Speaks MCP over stdio. Configure your LLM client (Claude Desktop, Claude Code, etc.) to launch it as a tool server.

### Environment variables

| Var | Default | Effect |
|-----|---------|--------|
| `DWF_BACKEND` | `pydwf` | Backend implementation. Set to `fake` for testing without hardware. |
| `DWF_WORKSPACE` | (cwd) | Workspace directory. Capture artifacts (`.npz`, `.parquet`, `.vcd`) are written under `<workspace>/captures/`. If unset, falls back to the current working directory — useful to pin to `/tmp/dwf` when launched from an MCP client that has an arbitrary cwd. |
| `DWF_ENABLE_VCD` | (autodetect) | Set to `1` to force VCD output writer on (requires `pip install dwf-mcp[vcd]`), `0` to disable. Default: enabled if `pyvcd` importable. |

## Tool surface (66 tools)

| Group | Tools |
|---|---|
| Meta | `waveforms.{open,close,status,list_pins}` |
| Power | `supply.{set,enable,disable,read}` (safety-gated) |
| Analog in | `scope.{configure,capture,set_trigger,record_start,record_status,record_stop}` |
| Analog out | `awg.{configure,upload_custom,start,stop}` |
| Digital in | `logic.{configure,capture,set_trigger,record_start,record_status,record_stop}` |
| Digital out | `pattern.{configure,start,stop}`, `dio.{set_direction,set,read}` |
| Measurement | `dmm.measure` |
| Protocol masters | `i2c.{configure,scan,write,read,write_read}`, `spi.{configure,transfer,write,read}`, `uart.{configure,write,read}`, `can.{configure,send,receive}` |
| Protocol sniff (blocking, engine-mode) | `sniff.{i2c,uart,can}` |
| Protocol sniff (async, observe-mode) | `sniff.{i2c,uart,can,spi}_{start,status,stop}` |
| Post-process decode | `decoder.{i2c,uart,can,spi}` |

Every output-driving call (`supply.enable`, `awg.start`, `pattern.start`) routes through `device.gate_output`, which checks against the active `SafetyPolicy` and logs to `dwf-safety.log`.

## Worked examples

### 1. Acquire a square wave on scope CH1, persist to npz

```jsonc
// 1. Open the device, set a low supply voltage cap so we can't fry anything.
{"name": "waveforms.open", "arguments": {"supply_max_voltage_pos": 3.3}}

// 2. Bring up a 3.3V rail and drive a 1 kHz square wave on AWG W1.
{"name": "supply.set", "arguments": {"channel": "vpos", "voltage": 3.3}}
{"name": "supply.enable", "arguments": {"channel": "vpos"}}
{"name": "awg.configure", "arguments": {
  "channel": 1, "function": "square", "frequency_hz": 1000,
  "amplitude": 1.5, "offset": 1.5
}}
{"name": "awg.start", "arguments": {"channel": 1}}

// 3. Configure scope CH1 ±2.5V around the W1 trace and capture.
{"name": "scope.configure", "arguments": {
  "channel": 1, "range_v": 2.5, "offset_v": 1.5, "coupling": "DC"
}}
{"name": "scope.set_trigger", "arguments": {
  "source": "channel1", "level_v": 1.65, "slope": "rising", "auto": true
}}
{"name": "scope.capture", "arguments": {
  "sample_rate_hz": 1e6, "n_samples": 8192,
  "output_path": "/tmp/square_capture.npz"
}}
// → {"artifact_path": "/tmp/square_capture.npz",
//    "sidecar_path": "/tmp/square_capture.json", ...}
```

The LLM can then load the npz and reason about the waveform, or call further tools to act on what it sees.

### 2. Sniff an I2C bus concurrently with active master

This is the headline Stage 5 capability: `sniff.i2c_start/stop` (observe-mode) uses DigitalIn, while `i2c.scan/write/read` use the protocol engine — they don't conflict.

```jsonc
{"name": "waveforms.open", "arguments": {}}

// Start observing the bus on DIO0/DIO1 with a 5s ceiling.
{"name": "sniff.i2c_start", "arguments": {
  "sda_pin": "dio0", "scl_pin": "dio1",
  "clock_hz": 100000, "max_duration_s": 5.0
}}
// → {"sniff_id": "..."}

// Meanwhile, fire an active master scan on the SAME wires.
{"name": "i2c.configure", "arguments": {
  "sda_pin": "dio0", "scl_pin": "dio1", "clock_hz": 100000
}}
{"name": "i2c.scan", "arguments": {}}

// Stop the sniff, get back the decoded transactions.
{"name": "sniff.i2c_stop", "arguments": {"sniff_id": "..."}}
// → {"artifact_path": "...parquet", "count": 128, "summary": {...}, ...}
```

**Long captures (`stream_decode: true`).** By default each `sniff.*_start` enforces a 32 MB raw-sample ceiling via `check_memory_cap` — at AD3 sample rates a multi-second capture saturates quickly. Pass `stream_decode: true` to opt into a live-decode path that feeds chunks through the protocol decoder during capture instead of buffering raw samples. The 32 MB cap is skipped (max duration is the only remaining bound), but if the decoder can't keep up, `lost_samples` will increment — check it in the stop result.

### 3. Record raw logic + decode any protocol after the fact

`logic.record_start/stop` produces a raw DIO npz; the `decoder.*` tools then run software state machines over it. You can decode the same capture as multiple protocols, decide on parameters after seeing the data, etc.

```jsonc
{"name": "logic.configure", "arguments": {
  "pins": ["dio0", "dio1", "dio2", "dio3"],
  "sample_rate_hz": 10e6, "buffer_size": 16384
}}
{"name": "logic.record_start", "arguments": {"duration_s": 2.0}}
// ... wait for done via record_status ...
{"name": "logic.record_stop", "arguments": {"record_id": "..."}}
// → {"artifact_path": "/workspace/logic_<id>.npz", ...}

{"name": "decoder.spi", "arguments": {
  "capture_path": "/workspace/logic_<id>.npz",
  "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio2", "cs_pin": "dio3",
  "mode": 0, "bit_order": "msb"
}}
// → {"artifact_path": ".../decoder_spi_<id>.parquet", "count": N, ...}
```

### 4. Idle timeout (auto-release hardware)

The server tracks `_last_activity` per tool call and closes the device after `idle_timeout_s` (default 10 min). The next `call_tool` invocation returns `{"error": {"type": "DwfDeviceLost", ...}}`. The LLM can re-`waveforms.open` to recover.

## Architecture

Three layers:

- **Backend** (`backends/{fake,pydwf_backend}.py` behind `DwfBackend` ABC) wraps the C SDK. The fake backend is used for unit tests; the pydwf backend talks to real hardware.
- **Instruments** (`instruments/*.py` behind `Instrument` ABC) own the per-domain semantics: pin claims, safety gating, artifact writing, lifecycle. Each instrument exposes a `tools: dict[str, (method_name, schema)]` for the dispatcher.
- **Server** (`server.py`) is the MCP entry point. `DwfMcpApp.call_tool` dispatches by `<instrument>.<tool>` name, runs the device's idle ticker, converts known exception types (`SafetyViolation`, `PinAllocationError`, `DwfDeviceLost`, `InstrumentNotConfigured`) into `{"error": {...}}` result dicts.

`PinAllocator` enforces mutual exclusion on physical DIO pins + virtual resources (`i2c_engine`, `uart_engine`, etc.), and supports a `claim_observe` mode that lets an observer (e.g. `sniff.spi_start`) coexist with an exclusive writer (e.g. `i2c.configure`) on the same wires.

`SafetyPolicy` is set at `waveforms.open` time and bounds supply voltage / current, AWG amplitude, pattern voltage. Every output-enabling call (the `gate_output` invocations in instruments) records to `dwf-safety.log` whether accepted or rejected.

## Testing

```bash
pytest tests/unit                 # 338 tests, no hardware
pytest tests/hardware -m hardware # 24 tests, requires AD3 (+ Jumperless V5 for protocol sniff tests)
ruff check src/ tests/
mypy src/
```

`tests/hardware/conftest.py` includes a `jumperless` fixture that auto-routes signals via a [Jumperless V5](https://github.com/Architeuthis-Flux/Jumperless) breadboard. When Jumperless isn't available, tests prompt the user to make connections by hand (or skip if the test depends on RP2350B-driven stimulus).

## Repository layout

```
src/dwf_mcp/
  server.py              # DwfMcpApp + stdio MCP entry point
  device.py              # DwfDevice: lazy open / idle / unplug recovery
  policy.py              # SafetyPolicy
  allocator.py           # PinAllocator, claim / claim_observe
  artifacts.py           # ArtifactWriter (npz / parquet + JSON sidecar)
  streaming.py           # RecordingSession + record_loop (shared by scope/logic)
  backend.py             # DwfBackend ABC
  backends/
    fake.py              # in-memory backend for unit tests
    pydwf_backend.py     # real AD3 via pydwf
  instruments/
    {scope,awg,supply,logic,pattern,dio,dmm,i2c,spi,uart,can,sniff}.py
    decoder/
      base.py            # Decoder ABC + per-protocol dataclasses
      {spi,i2c,uart,can}.py
      __init__.py        # decoder.{spi,i2c,uart,can} MCP tools
    _async_sniff.py      # shared observe-mode session infrastructure

docs/superpowers/specs/   # stage design docs
docs/superpowers/plans/   # stage implementation plans
docs/plans/               # earlier per-stage plans
```

## License

MIT.
