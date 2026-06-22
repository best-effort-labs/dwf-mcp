# dwf-mcp

**Drive a real electronics bench by conversation.**

An [MCP](https://modelcontextprotocol.io) server that exposes the [Digilent WaveForms SDK](https://digilent.com/reference/test-and-measurement/waveforms/waveforms-sdk/start) to LLM agents. It turns an Analog Discovery (and compatible Digilent instruments) into a tool surface a conversational model can actually wield — oscilloscope, arbitrary waveform generator, programmable supply, logic analyzer, **spectrum / Bode / impedance analyzers**, protocol masters, live bus sniffers, and post-process decoders — behind one stable, safety-gated API.

The pitch in one line: given a wired-up circuit, an agent can generate a stimulus, capture the response, and reason about real volts — not a simulation.

## Not just tools — expertise

Most MCP servers hand a model a pile of tool schemas and hope it figures out the rest. dwf-mcp also ships a **measurement cookbook**: 19 recipes that map an *intent* ("measure a filter's gain and phase", "sniff an I2C bus while a master is active", "find a component's impedance") to the exact tool sequence, the wiring, and the **validated math** for interpreting the result. It's served as MCP resources (`dwf://cookbook/*`), and every one of the 75 tools carries a one-line description. An agent gets the know-how to operate the bench, not just its API.

## What it can do

- **Time domain** — triggered/free-run scope capture, long streaming records past the hardware buffer, AWG (built-in functions *and* arbitrary `.npy` waveforms), programmable dual supply, and a high-accuracy DMM.
- **Frequency domain** — FFT spectrum analyzer, Bode gain/phase sweeps, and full complex-impedance analysis (`|Z|`, phase, R/X, C/L, Q/D vs. frequency). All three hardware-validated.
- **Digital I/O** — logic analyzer (buffer + streaming record), pattern generator, and per-pin DIO with drive-strength, pull-mode, and I/O-voltage control on devices that support it.
- **Protocol masters** — I2C, SPI, UART, and CAN engines that talk to real chips without bit-banging.
- **Protocol sniffing** — passive I2C/SPI/UART/CAN observers that **coexist with active masters on the same wires** (DigitalIn-based observe mode), plus software decoders that run over any recorded logic capture after the fact.
- **Safety by construction** — output-enabling calls (supply, AWG, pattern, DIO drive) pass a `SafetyPolicy` gate (voltage/current/amplitude caps set at open time), and every decision is logged to `dwf-safety.log`.

## Supported hardware

The server probes the connected device at open time and gates tools to its real capabilities.

| Device | Role | Status |
|--------|------|--------|
| **Analog Discovery 3** | Full mixed-signal (scope, AWG, supply, logic, DIO, protocols) | Primary target — fully hardware-validated |
| **ADP2230** | Full mixed-signal; DIO drive-strength + pull control (fixed 3.3 V LVCMOS I/O); 1 user AWG (W1) | Hardware-validated |
| **Digital Discovery** | Digital-only (logic analyzer, pattern, DIO); adjustable 1.2–3.3 V DIO levels | Hardware-validated |
| **Analog Discovery 1 / 2** | Mixed-signal | Compatible (AD2 streaming-record quirk known) |

Hardware-test auto-wiring uses a [Jumperless V5](https://github.com/Architeuthis-Flux/Jumperless) programmable breadboard when present.

## Install

```bash
git clone <repo>
cd dwf-mcp
pip install -e ".[vcd,dev]"
```

Requires the WaveForms runtime (which provides `libdwf`) and Python 3.11+. macOS: `brew install --cask digilent-waveforms`. Linux: see below.

### Linux

Verified end-to-end on Ubuntu 24.04 (x86_64): full unit suite + all hardware tests against an AD3 + Jumperless V5. Two Digilent packages are required — both download without a login from Digilent's [previous-releases pages](https://digilent.com/reference/software/waveforms/waveforms-3/previous-versions):

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

Speaks MCP over stdio. Configure your LLM client (Claude Desktop, Claude Code, etc.) to launch it as a tool server. On first contact, point the agent at `dwf://cookbook/index` — the server's MCP instructions already do this.

### Environment variables

| Var | Default | Effect |
|-----|---------|--------|
| `DWF_BACKEND` | `pydwf` | Backend implementation. Set to `fake` for testing without hardware. |
| `DWF_WORKSPACE` | (cwd) | Workspace directory. Capture artifacts (`.npz`, `.parquet`, `.vcd`) are written under `<workspace>/captures/`. If unset, falls back to the current working directory — useful to pin to `/tmp/dwf` when launched from an MCP client that has an arbitrary cwd. |
| `DWF_ENABLE_VCD` | (autodetect) | Set to `1` to force VCD output writer on (requires `pip install dwf-mcp[vcd]`), `0` to disable. Default: enabled if `pyvcd` importable. |

## Tool surface (75 tools)

| Group | Tools |
|---|---|
| Meta | `waveforms.{open,close,status,list_pins}` |
| Power | `supply.{set,enable,disable,read}` (safety-gated) |
| Analog in | `scope.{configure,capture,set_trigger,record_start,record_status,record_stop}` |
| Analog out | `awg.{configure,upload_custom,start,stop}` |
| Frequency domain | `spectrum.{configure,measure,transform}`, `bode.{configure,measure}`, `impedance.{configure,measure}` |
| Digital in | `logic.{configure,capture,set_trigger,record_start,record_status,record_stop}` |
| Digital out | `pattern.{configure,start,stop}`, `dio.{set_direction,set,read,set_drive,set_pull,set_voltage}` |
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

// 3. Configure scope CH1 (range/rate/buffer live on configure), set trigger, capture.
{"name": "scope.configure", "arguments": {
  "channels": [1], "range_v": 5.0, "offset_v": 1.5, "coupling": "DC",
  "sample_rate_hz": 1e6, "buffer_size": 8192
}}
{"name": "scope.set_trigger", "arguments": {
  "source": "detector_analog_in", "channel": 1,
  "level_v": 1.65, "condition": "Rising"
}}
{"name": "scope.capture", "arguments": {"output_path": "/tmp/square_capture.npz"}}
// → {"path": "/tmp/square_capture.npz",
//    "sidecar_path": "/tmp/square_capture.json", "summary": {...}}
```

The LLM can then load the npz and reason about the waveform, or call further tools to act on what it sees.

### 2. Characterize a filter — gain and phase vs. frequency (Bode)

`bode` and `impedance` sweep the AWG and ratiometrically capture two scope channels; `spectrum` does a single-channel FFT instead. For a Bode plot, drive the filter input from W1, watch the input on CH1 (reference) and the output on CH2 (DUT).

```jsonc
{"name": "waveforms.open", "arguments": {}}

// Sweep 100 Hz → 100 kHz, 50 log-spaced points; CH1 = filter in, CH2 = filter out.
{"name": "bode.configure", "arguments": {
  "start_hz": 100, "stop_hz": 100000, "points": 50, "spacing": "log",
  "drive_channel": 1, "ref_channel": 1, "dut_channel": 2,
  "amplitude_v": 1.0
}}
{"name": "bode.measure", "arguments": {}}
// → {"path": ".../bode_<id>.npz", "sidecar_path": ".../bode_<id>.json",
//    "summary": {"point_count": 50, "gain_db_min": ..., "gain_db_max": ..., ...}}
```

The npz carries gain (dB) and phase (deg) per frequency; the agent reads off the slope and resonance directly, and the −3 dB corner via the cookbook's validated `bode_f3db` formula. `impedance.configure`/`measure` follow the same shape (add a series reference resistor) and return `|Z|`, phase, and the derived R/X, C/L, Q/D — see `dwf://cookbook/freq-domain` for the recipes and the math.

### 3. Sniff an I2C bus concurrently with active master

`sniff.i2c_start/stop` (observe-mode) uses DigitalIn, while `i2c.scan/write/read` use the protocol engine — they don't conflict on the same wires.

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

### 4. Drive an active SPI master and read back the response

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
// → {"sent": [171, 205, 239, 18], "received": [171, 205, 239, 18]}
```

For asymmetric reads (write-then-read with a turnaround), use `spi.write` + `spi.read` separately with `assert_cs: false` on the first call so CS stays low across both.

### 5. Long observe-mode capture with live decode (`stream_decode: true`)

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

### 6. Record raw logic + decode any protocol after the fact

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
# 1. Convert the npz channels to sigrok's binary input format
python3 -c "
import numpy as np
npz = np.load('/workspace/logic_<id>.npz')   # one 1-D uint8 array per pin name
pins = ['dio0', 'dio1', 'dio2', 'dio3']       # the captured pins
packed = np.zeros(npz[pins[0]].shape[0], dtype=np.uint8)
for bit, p in enumerate(pins):                # bit i = pins[i] (D0 = dio0)
    packed |= (npz[p].astype(np.uint8) & 1) << bit
open('/tmp/cap.bin', 'wb').write(packed.tobytes())
"

# 2. Decode (here: DS18B20 OneWire on D0 = dio0; substitute your protocol + pin map)
sigrok-cli -I binary:numchannels=4:samplerate=10000000 -i /tmp/cap.bin \
  -P onewire_link:owr=0,onewire_network,onewire_transport,ds18b20 \
  -A ds18b20

# 3. Parse annotation lines (one per decoded event) into transactions
```

`sigrok-cli -L` lists all available decoders; `sigrok-cli --show-pd protocol_id` describes one. The ~100ms-per-call startup is fine for one-shot post-process. If you need this often, wrap steps 1-3 in a helper script.

### 7. Play an arbitrary waveform on AWG W1 from a .npy file

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

### 8. Idle timeout (auto-release hardware)

The server tracks `_last_activity` per tool call and closes the device after `idle_timeout_s` (default 10 min). The next `call_tool` invocation returns `{"error": {"type": "DwfDeviceLost", ...}}`. The LLM can re-`waveforms.open` to recover.

## Documentation

- **Measurement cookbook** — served live as MCP resources; start at `dwf://cookbook/index`:

  | Resource URI | Contents |
  |---|---|
  | `dwf://cookbook/index` | Intent → recipe map, quick-start, and a one-liner for every tool |
  | `dwf://cookbook/freq-domain` | Spectrum, Bode, impedance recipes + the validated math |
  | `dwf://cookbook/time-domain` | Scope capture, AWG stimulus, supply, DMM, GPIO, record, THD/SNR |
  | `dwf://cookbook/protocols` | I2C, SPI, UART, CAN — master and sniff recipes |
  | `dwf://cookbook/bench` | Session setup, power supply, pattern generator |

  The source lives at `src/dwf_mcp/cookbook/`. The server's MCP `instructions` point agents at `dwf://cookbook/index` before they call any measurement tool.

- [docs/architecture.md](docs/architecture.md) — layered design, safety model, pin allocator, streaming/record, observe-mode sniff, backend contract. For developers extending the server and agents needing a deeper mental model.
- [docs/troubleshooting.md](docs/troubleshooting.md) — known limitations (including device unplug), Linux/VM hardware-setup gotchas, safety-policy behavior, the sniff memory cap, and a common-error table.

## Architecture

Three layers:

- **Backend** (`backends/{fake,pydwf_backend}.py` behind `DwfBackend` ABC) wraps the C SDK. The fake backend is used for unit tests; the pydwf backend talks to real hardware.
- **Instruments** (`instruments/*.py` behind `Instrument` ABC) own the per-domain semantics: pin claims, safety gating, artifact writing, lifecycle. Each instrument exposes a `tools: dict[str, (method_name, schema)]` for the dispatcher.
- **Server** (`server.py`) is the MCP entry point. `DwfMcpApp.call_tool` dispatches by `<instrument>.<tool>` name, runs the device's idle ticker, converts known exception types (`SafetyViolation`, `PinAllocationError`, `DwfDeviceLost`, `InstrumentNotConfigured`) into `{"error": {...}}` result dicts. `build_server` additionally wires the cookbook MCP resources and tool descriptions.

`PinAllocator` enforces mutual exclusion on physical DIO pins + virtual resources (`i2c_engine`, `uart_engine`, etc.), and supports a `claim_observe` mode that lets an observer (e.g. `sniff.spi_start`) coexist with an exclusive writer (e.g. `i2c.configure`) on the same wires.

`SafetyPolicy` is set at `waveforms.open` time and bounds supply voltage / current, AWG amplitude, pattern voltage. Every output-enabling call (the `gate_output` invocations in instruments) records to `dwf-safety.log` whether accepted or rejected.

## Testing

```bash
pytest -m 'not hardware'           # 619 tests, no hardware (fake backend)
pytest -m hardware                 # 46 tests, requires a Digilent device (+ Jumperless V5 for wired protocol/sniff tests)
ruff check src/ tests/
mypy src/
```

Hardware tests self-classify with `standalone` (device-only) and `wired` (needs connections) markers, and gate on declared requirements via `@pytest.mark.requires(instruments=..., pins=...)`. `tests/hardware/conftest.py` includes a `jumperless` fixture that auto-routes signals via a [Jumperless V5](https://github.com/Architeuthis-Flux/Jumperless) breadboard; wired tests auto-skip when none is attached. Select a specific device with `DWF_TEST_SERIAL=<serial>`.

## Repository layout

```
src/dwf_mcp/
  server.py              # DwfMcpApp + build_server + stdio MCP entry point
  device.py              # DwfDevice: lazy open / idle close / safety gate
  policy.py              # SafetyPolicy
  allocator.py           # PinAllocator, claim / claim_observe
  artifacts.py           # ArtifactWriter (npz / parquet + JSON sidecar)
  streaming.py           # RecordingSession + record_loop (shared by scope/logic)
  formulae.py            # validated derived measurements (thd / snr_db / bode_f3db)
  tool_descriptions.py   # one-line description per tool (drift-tested vs. app._tools)
  backend.py             # DwfBackend ABC
  backends/
    fake.py              # in-memory backend for unit tests
    pydwf_backend.py     # real device via pydwf
  cookbook/              # measurement cookbook (loader + recipe docs → MCP resources)
  instruments/
    {scope,awg,supply,logic,pattern,dio,dmm,i2c,spi,uart,can,sniff}.py
    {spectrum,bode,impedance}.py
    decoder/
      base.py            # Decoder ABC + per-protocol dataclasses
      {spi,i2c,uart,can}.py
      __init__.py        # decoder.{spi,i2c,uart,can} MCP tools
    _async_sniff.py      # shared observe-mode session infrastructure
```

## License

MIT.
