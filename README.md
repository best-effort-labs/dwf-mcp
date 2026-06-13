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

Requires the WaveForms runtime (which provides `libdwf`) and Python 3.11+. macOS: `brew install --cask digilent-waveforms`. Linux: see below.

### Linux

Verified end-to-end on Ubuntu 24.04 (x86_64): full unit suite + all 24 hardware tests against an AD3 + Jumperless V5. Two Digilent packages are required — both download without a login from Digilent's [previous-releases pages](https://digilent.com/reference/software/waveforms/waveforms-3/previous-versions):

```bash
curl -fsSO "https://files.digilent.com/Software/Adept2%20Runtime/2.27.9/digilent.adept.runtime_2.27.9-amd64.deb"
curl -fsSO "https://files.digilent.com/Software/Waveforms/3.25.1/digilent.waveforms_3.25.1_amd64.deb"
sudo mkdir -p /usr/share/desktop-directories   # headless only: WaveForms postinst runs xdg-desktop-menu, which exits nonzero without this
sudo apt install ./digilent.adept.runtime_*.deb ./digilent.waveforms_*.deb
```

- `libdwf.so` comes from the WaveForms package (`pydwf` is a ctypes wrapper around it, no compiled code of its own); the Adept Runtime provides the USB plumbing and the udev rules that make the AD3 accessible without root.
- The `mkdir` line is the only headless accommodation needed — without it the package lands half-configured (`iF` in `dpkg -l`) and `apt` reports an error.

If using a Jumperless V5 for hardware-test auto-wiring:

- Add yourself to `dialout` for `/dev/ttyACM*` access: `sudo usermod -aG dialout $USER` (takes effect at next login).
- **Minimal/cloud kernels lack `cdc_acm`.** Ubuntu cloud images ship the `virtual` kernel flavor, which omits USB serial drivers entirely — the Jumperless enumerates on the bus but no `/dev/ttyACM*` nodes ever appear, with nothing in dmesg to say why. Fix: `sudo apt install linux-modules-extra-$(uname -r)`.
- Port discovery needs no changes: Linux exposes per-interface product strings (e.g. `ttyACM2` → "Jumperless V5 - JL Micropython REPL"), so the sorted-port-order assumption from macOS holds.

### Deploying in a VM (QEMU / KVM / Proxmox)

Verified on Proxmox 8 with both devices USB-passed-through. Hard-won notes:

- **The CPU model must expose x86-64-v2.** NumPy ≥ 2 aborts on import under the default `qemu64`/`kvm64` model (`NumPy was built with baseline optimizations: (X86_V2) but your machine doesn't support...`). On Proxmox set `cpu: host`.
- Pass devices by vendor:product — AD3 = `1443:7003` (Digilent's own VID, not FTDI), Jumperless V5 = `1d50:acab` — with `usb3=1` on the AD3 entry. USB config changes only apply on a full VM stop/start, not a guest-initiated reboot.
- **The AD3's host-side USB link must not be SuperSpeed.** Through some paths (observed with a fiber-optic USB extender) the AD3 links at USB 3.0, and QEMU's emulated xHCI then silently black-holes bulk transfers: device *enumeration still succeeds* (descriptor reads only), but `FDwfDeviceOpen` blocks forever with a completely clean guest dmesg. Plugged directly into a host port the link comes up Hi-Speed (480M) and everything works. Corollary: a successful `enumerateDevices()` does not prove the passthrough is healthy — verify `open()` too.
- After a hung open, the AD3 can wedge and vanish from the guest until physically unplugged and replugged.
- Emulated-USB latency spikes can occasionally overflow streaming-capture buffers: a one-off nonzero `lost_samples` failure in the hardware suite (e.g. `logic.record` at 1 MHz) is more likely flake than regression in a VM — rerun before debugging.

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
  "channel": 1, "function": "Square", "frequency_hz": 1000,
  "amplitude_v": 1.5, "offset_v": 1.5
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

### 3. Drive an active SPI master and read back the response

The four protocol masters (`i2c`, `spi`, `uart`, `can`) wrap the AD3's protocol engines so the LLM can talk to real chips without bit-banging. SPI shown here; the others follow the same `configure` → `transfer/write/read` shape.

```jsonc
{"name": "waveforms.open", "arguments": {}}

// Pin out the bus. With MISO tied to MOSI on the breadboard, transfer() loops bytes back.
{"name": "spi.configure", "arguments": {
  "clk_pin": "dio0", "mosi_pin": "dio1", "miso_pin": "dio1", "cs_pin": "dio2",
  "frequency_hz": 1_000_000, "mode": 0, "bit_order": "msb"
}}

// Drive CS low, clock out 4 bytes, capture 4 bytes from MISO, drive CS high.
{"name": "spi.transfer", "arguments": {"data": [0xAB, 0xCD, 0xEF, 0x12], "assert_cs": true}}
// → {"received": [171, 205, 239, 18], "byte_count": 4}
```

For asymmetric reads (write-then-read with a turnaround), use `spi.write` + `spi.read` separately with `assert_cs: false` on the first call so CS stays low across both.

### 4. Long observe-mode capture with live decode (`stream_decode: true`)

By default each `sniff.*_start` enforces a 32 MB raw-sample ceiling via `check_memory_cap` — at AD3 sample rates a multi-second capture saturates quickly. Pass `stream_decode: true` to feed chunks through the protocol decoder during capture instead of buffering raw samples. The 32 MB cap is skipped (max duration is the only remaining bound).

```jsonc
{"name": "waveforms.open", "arguments": {}}

// 60-second I2C capture — without stream_decode this would be rejected by the cap.
{"name": "sniff.i2c_start", "arguments": {
  "sda_pin": "dio0", "scl_pin": "dio1",
  "clock_hz": 400_000, "max_duration_s": 60.0,
  "stream_decode": true
}}
// → {"sniff_id": "..."}

// While the capture runs, status reports incremental progress.
{"name": "sniff.i2c_status", "arguments": {"sniff_id": "..."}}
// → {"done": false, "samples_received": 6_200_000, "lost_samples": 0}

// Stop. Transactions were decoded live, so the stop result is ready immediately.
{"name": "sniff.i2c_stop", "arguments": {"sniff_id": "..."}}
// → {"artifact_path": "...parquet", "count": 1147, "lost_samples": 0, "summary": {...}, ...}
```

If the decoder can't keep up with the chunk rate (rare for I2C/UART at typical rates, more likely for SPI/CAN at the upper end), the backend's record buffer fills and samples drop — `lost_samples > 0` in the stop result is the signal. The captured-and-decoded transactions are still valid; only the dropped raw bytes weren't seen. Reduce `sample_rate_hz` or `max_duration_s` (or fall back to `stream_decode: false` with the cap).

### 5. Record raw logic + decode any protocol after the fact

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

**For protocols beyond the built-in four (i2c, uart, spi, can):** the captured `.npz` can be decoded with [`sigrok-cli`](https://sigrok.org/wiki/Sigrok-cli) (which ships ~150 libsigrokdecode protocols — DS18B20, MDIO, JTAG, USB-LS, OneWire, SDQ, etc.). There is no dedicated MCP tool — a calling agent with shell + Python access can:

```bash
# 1. Convert the npz channels to sigrok's binary input format (~5 lines)
python3 -c "
import numpy as np
arr = np.load('/workspace/logic_<id>.npz')['data']  # shape (n_samples, 16) uint8 per-pin
# pack the active channels into one byte per sample, LSB = lowest pin index
packed = np.packbits(arr[:, :8][:, ::-1], axis=1).squeeze().tobytes()
open('/tmp/cap.bin', 'wb').write(packed)
"

# 2. Decode (here: DS18B20 OneWire on dio0; substitute your protocol + pin map)
sigrok-cli -I binary:numchannels=8:samplerate=10000000 -i /tmp/cap.bin \
  -P onewire_link:owr=0,onewire_network,onewire_transport,ds18b20 \
  -A ds18b20

# 3. Parse annotation lines (one per decoded event) into transactions
```

`sigrok-cli -L` lists all available decoders; `sigrok-cli --show-pd protocol_id` describes one. The ~100ms-per-call startup is fine for one-shot post-process. If you need this often, wrap steps 1-3 in a helper script.

### 6. Play an arbitrary waveform on AWG W1 from a .npy file

`awg.configure` covers the built-in functions (Sine/Square/Triangle/RampUp/RampDown/DC/Noise). For anything else — recorded data, a custom envelope, a PWM-encoded message — use `awg.upload_custom`. Samples come from a `.npy` file on the server's filesystem, must be 1-D, and must be in the range [-1.0, 1.0] (they're scaled by `amplitude_v` at upload time).

```jsonc
{"name": "waveforms.open", "arguments": {}}

// Author the waveform offline (numpy, Python, ...) and save to disk as 1-D float64:
//   import numpy as np
//   t = np.linspace(0, 1, 4096, endpoint=False)
//   samples = np.sin(2*np.pi*5*t) * np.exp(-3*t)   // 5 Hz damped sine
//   np.save("/tmp/damped_sine.npy", samples.astype(np.float64))

{"name": "awg.upload_custom", "arguments": {
  "channel": 1,
  "samples_npy_path": "/tmp/damped_sine.npy",
  "amplitude_v": 2.0
}}
// → {"uploaded": true, "channel": 1, "n_samples": 4096}

{"name": "awg.start", "arguments": {"channel": 1}}
// W1 now plays the damped sine, peak ±2.0 V.

{"name": "awg.stop", "arguments": {"channel": 1}}
```

The waveform loops indefinitely until `awg.stop` or `waveforms.close`. The playback frequency depends on the AD3's hardware sample clock; configure that separately via `awg.configure` if you need a specific repetition rate.

### 7. Idle timeout (auto-release hardware)

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
```

## License

MIT.
