# dwf-mcp — Design

**Date:** 2026-06-02
**Status:** Validated through brainstorming; not yet implemented.

## Purpose

An MCP server that exposes the Digilent WaveForms SDK (DWF) to a Claude session so that, while working on a project, Claude can drive an Analog Discovery 3 to do active protocol I/O (I2C/SPI/UART/CAN), capture analog and digital traces, decode protocol traffic, and inspect the results — without leaving the conversation.

Mac first; Linux follows for free as long as no platform paths get hardcoded.

## Scope decisions

- **Target hardware:** Analog Discovery 3 (AD3). Other Digilent devices later via the same backend abstraction.
- **Runtime:** Python, using the `mcp` SDK (stdio transport) and `pydwf` (official maintained wrapper around `libdwf`).
- **Session model:** persistent device session, lazy-opened on first use, explicit `close` plus an idle timeout. Single device per server instance.
- **Pin model:** pin-level resource arbitration. Instruments declare which DIO/analog pins they claim at configure time; overlaps rejected by the allocator before any DWF call. AD3 hard constraints (shared clock domains, co-sampled channels) modeled as `resource_groups` the allocator also checks.
- **Data return:** capture results write to disk; tool results return file paths plus a summary. No raw samples inline. Default location is an OS temp dir, overridable at open time (workspace) or per call.
- **Protocols:** both active master and passive sniff+decode for I2C/SPI/UART/CAN.

## Architecture — layers

Bottom-up:

1. **Device layer** — `DwfDevice` wraps a `pydwf` handle. Lazy-opens, holds, closes on idle/explicit. Tracks claimed pins and instruments. Sits behind an `InstrumentHost` ABC so a non-Digilent backend (Saleae, MCC, custom FPGA) can slot in later under the same upper layers.
2. **Instrument layer** — one module per instrument behind a common `Instrument` ABC: `configure()`, `arm()`, `read()`, `release()`.
3. **Tool layer** — thin MCP tool wrappers. Argument validation, dispatch to instruments, artifact bookkeeping. No business logic.
4. **Artifact layer** — write paths, serialization (npz / parquet / optional vcd), JSON sidecars, summary generation.

### Extensibility

New interface = one file under `instruments/`, implementing `Instrument` + exposing a `register(server, registry)` entry point. `instruments/__init__.py` imports the module; tool surface auto-derives. A future CAN sniffer, SWD master, or a swap to a different host device reuses every layer above the one being added.

### Portability

`pydwf` locates `libdwf.dylib` via Digilent's macOS framework and `libdwf.so` on Linux automatically. No path hardcoding, no platform branches expected.

## Session & device lifecycle

- **Open:** `waveforms.open(workspace_dir?, idle_timeout_s?, device_serial?, safety_policy_kwargs...)` enumerates, opens the first AD3 (or matching serial), returns device info. Workspace defaults to `tempfile.mkdtemp(prefix="dwf-")`. Re-open is idempotent.
- **Idle close:** background watcher closes the handle after `idle_timeout_s` of no tool calls (default ~10 min). Next call re-opens transparently. `force_keep_open=true` disables for long benches.
- **Explicit close:** `waveforms.close()` releases handle and clears all state.
- **Status:** `waveforms.status()` returns `{open, device, claimed_pins, claimed_instruments, idle_remaining_s, active_safety_policy, last_capture_path}`.
- **Hot-unplug:** the next DWF call surfaces `WaveformsDeviceLost`; session marked dead, `waveforms.open` resumes.
- **Crash safety:** no state file. OS handle cleanup on process death is sufficient.
- **Concurrency:** single-threaded DWF access guarded by an `asyncio.Lock`.

## Safety layer

Latched per session at open time, immutable until close. Any tool that sets a voltage, amplitude, or output enable gates through `policy.check()` before touching DWF.

```
waveforms.open(
  supply_max_voltage_pos=3.3,
  supply_max_voltage_neg=-3.3,
  supply_max_current=0.5,
  awg_max_amplitude=3.3,
  pattern_voltage="3.3",
  require_explicit_enable=True,
)
```

- Violations raise `WaveformsSafetyError` naming requested vs. allowed.
- Mid-session changes require `close` + `open` — no escape hatch.
- Supply: `set` writes voltage but never enables output; `enable` and `disable` are explicit; `read` reports requested vs. measured V/I. Optional `current_trip` auto-disables on sustained overcurrent.
- Auto-disable on session close (any cause), unplug, server shutdown, or safety error.
- Every output-enable, voltage change, and trip writes to `<workspace>/dwf-safety.log` with timestamp.
- `waveforms.status()` reports active policy and every output's enabled state.

## Tool surface

All tools accept a `description?` field stored in the capture sidecar.

### Scope (analog in)
- `scope.configure(channels, range_v, offset_v, coupling, sample_rate_hz, buffer_size)`
- `scope.set_trigger(source, channel, level_v, edge|condition, position_s, timeout_s, trigger_in_pin?, trigger_out_pin?)`
- `scope.capture(output_path?)` → `{path, sidecar_path, summary: {min, max, mean, rms, freq_estimate, glitch_count, sample_rate, trigger_time}}`
- `scope.record(output_path?, duration_s, sample_rate_hz)` — streaming mode for long captures

### AWG (analog out)
- `awg.configure(channel, function, frequency_hz, amplitude_v, offset_v, phase_deg, symmetry, run_time_s?)`
- `awg.upload_custom(channel, samples_npy_path)`
- `awg.start(channel)` / `awg.stop(channel)` — safety-gated

### Logic (digital in / sniff)
- `logic.configure(pins, sample_rate_hz, buffer_size)`
- `logic.set_trigger(pattern|edge|protocol_aware, ..., trigger_in_pin?, trigger_out_pin?)`
- `logic.capture(output_path?, format=npz|vcd)` → path + per-channel edge counts
- `logic.record(output_path?, duration_s, ...)` — streaming

### Pattern (digital out)
- `pattern.configure(pin, function, frequency_hz, duty, idle_state)`
- `pattern.start(pin)` / `pattern.stop(pin)`

### DIO (bidirectional GPIO)
- `dio.set_direction(pin, in|out)`
- `dio.set(pin, state)` / `dio.read(pin)`

### DMM (single-shot voltmeter)
- `dmm.read(channel, mode=dc|ac|peak, samples?)`

### Active masters
- `i2c.configure(sda_pin, scl_pin, clock_hz, pullups)`
- `i2c.write(address, data)` / `i2c.read(address, length)` / `i2c.write_read(address, write, read_length)` / `i2c.scan()`
- `spi.configure(sclk, mosi, miso, cs, mode, bit_order, clock_hz)`
- `spi.transfer(cs_assert, write_bytes, read_length)`
- `uart.configure(tx_pin, rx_pin, baud, bits, parity, stop)`
- `uart.write(data)` / `uart.read(timeout_s, max_bytes)`
- `can.configure(tx_pin, rx_pin, bitrate)`
- `can.send(id, data, extended?)` / `can.receive(timeout_s, max_frames)`

### Decoder (passive, on a logic capture)
- `decoder.i2c(capture_path, sda_pin, scl_pin, output_path?)`
- `decoder.spi(capture_path, sclk_pin, mosi_pin, miso_pin, cs_pin, mode, output_path?)`
- `decoder.uart(capture_path, rx_pin, baud, ..., output_path?)`
- `decoder.can(capture_path, rx_pin, bitrate, output_path?)`
- Each returns `{path: parquet, sidecar_path, count, errors, summary: {first_n: [...]}}`

### Supply
- See safety layer.

### System
- `system.monitor()` → `{usb_voltage, usb_current, aux_voltage, aux_current, device_temp_c}`

### Meta
- `waveforms.open`, `waveforms.close`, `waveforms.status`, `waveforms.list_pins`

## Deferred (post-v1)

- SWD master
- AWG modulation (AM/FM/sweep)
- Network analyzer / impedance analyzer composite modes
- Custom bit-bang protocols via DigitalIn/Out primitives (the extensibility model already covers this when needed)

## Artifacts

```
<workspace>/
  dwf-safety.log
  captures/
    2026-06-02T14-32-08_scope_<uuid>.npz
    2026-06-02T14-32-08_scope_<uuid>.json
    2026-06-02T14-33-15_logic_<uuid>.npz
    2026-06-02T14-33-15_logic_<uuid>.vcd
    2026-06-02T14-33-22_i2c-decode_<uuid>.parquet
    2026-06-02T14-33-22_i2c-decode_<uuid>.json
```

- `.npz` — raw analog/digital samples; numpy-native, fast, compressed.
- `.parquet` — decoded transactions; column-oriented, queryable with `duckdb`.
- `.vcd` — optional logic output for PulseView/GTKWave.
- `.json` sidecar — always written: full configuration, safety policy snapshot at capture time, pin allocation, summary stats. Self-contained context for the capture.

Tool results return `{path, sidecar_path, summary}` — never raw samples inline.

## Project layout

```
dwf-mcp/
  pyproject.toml
  src/dwf_mcp/
    server.py
    device.py             # DwfDevice + InstrumentHost ABC
    policy.py             # SafetyPolicy
    allocator.py          # pin + resource-group allocator
    artifacts.py          # writers + sidecar
    instruments/
      __init__.py         # registry
      scope.py  awg.py  logic.py  pattern.py  dio.py  dmm.py
      i2c.py    spi.py    uart.py   can.py
      supply.py
      decoder/
        i2c.py  spi.py  uart.py  can.py
  tests/
    unit/  golden/  hardware/  integration/
```

## Testing strategy

- **Unit** — pin allocator, safety policy, artifact writer, decoders. No hardware; CI-friendly.
- **Decoder golden** — checked-in `.npz` fixtures of real bus traffic; decoder output must match committed parquet.
- **Hardware smoke** — `pytest -m hardware` against a plugged-in AD3 with a loopback harness (W1↔1+, DIO0↔DIO1, etc.). Run locally pre-release, not in CI.
- **MCP integration** — spawn the server via the MCP SDK's test client; exercise open→configure→capture→close with a mocked `DwfDevice`. Catches schema/serialization bugs.

## Open questions for implementation

- AD3-specific pin/resource-group table — confirm against the hardware reference manual rather than assumed from AD2.
- Decoder behavior on malformed transactions: parquet with `error` column vs. separate error sidecar. Resolve when writing the decoder module.
- VCD writer: hand-rolled vs. `vcd` PyPI package — decide on first logic-capture implementation.
