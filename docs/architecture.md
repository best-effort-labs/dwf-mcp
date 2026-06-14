# Architecture

This document explains how `dwf-mcp` is put together — the layers, the
cross-cutting systems (safety, pin allocation, streaming), and the contracts
between them. It is aimed at developers extending the server and at LLM agents
that need a mental model deeper than the tool list in the README.

## Layered design

```
MCP client (LLM / SDK)
        │  <instrument>.<tool> calls
        ▼
DwfMcpApp (server.py)        — dispatch, error mapping, lifecycle gating
        ▼
Instrument (instruments/*)   — per-domain semantics: claims, gating, artifacts
        ▼
DwfDevice (device.py)        — session: lazy-open, idle close, safety gate
        ▼
DwfBackend (backend.py)      — ABC; FakeBackend (tests) | PydwfBackend (hardware)
        ▼
libdwf / Analog Discovery 3
```

Each layer only knows about the one below it. A new instrument is one file under
`instruments/`; a different host device (another Digilent box, or a non-Digilent
analyzer) is one new `DwfBackend` implementation — everything above is reused.

## Server: `DwfMcpApp` (`server.py`)

The app holds the `DwfDevice`, the instrument registry, the lazily-instantiated
instrument instances, and the tool dispatch table. `call_tool(name, args)`:

1. Resolves `<instrument>.<tool>` to a handler.
2. For non-lifecycle tools, calls `device.require_open()` — so a closed,
   idle-expired, or never-opened device returns a clean `DwfDeviceLost` instead
   of a raw backend error. The lifecycle tools (`waveforms.open`,
   `waveforms.close`, `waveforms.status`) run regardless of device state.
3. Runs the device idle ticker and gives every live instrument a `tick_idle()`
   to reap background state (e.g. orphan sniff sessions).
4. Dispatches, then maps known exceptions
   (`SafetyViolation`, `PinAllocationError`, `DwfDeviceLost`,
   `InstrumentNotConfigured`) to `{"error": {type, message, details}}` result
   dicts. On `DwfDeviceLost` it also clears the instrument cache so a re-open
   starts clean. Unknown exceptions propagate unless the device is found to be
   gone (see [Device loss](troubleshooting.md#device-loss--unplug)).

Instruments are instantiated on first use and cached. Each declares a class-level
`tools: dict[str, (method_name, json_schema)]` from which the tool surface is
auto-derived — adding a method + schema entry exposes a new MCP tool.

## Device & session lifecycle (`device.py`)

One device per server instance. `DwfDevice` is a thin session wrapper:

- **Lazy open.** `waveforms.open(workspace_dir?, idle_timeout_s?, device_serial?,
  <safety policy kwargs>)` enumerates and opens the first AD3 (or a matching
  serial). Re-open is idempotent. Workspace defaults to an OS temp dir.
- **Idle close.** Every tool call stamps activity; `tick_idle()` closes the
  handle after `idle_timeout_s` (default 600 s) of inactivity, releasing all
  hardware. The next call re-opens transparently via `require_open`.
- **Explicit close.** `waveforms.close()` releases the handle and clears all
  instrument + allocator state.
- **`require_open()`** raises `DwfDeviceLost` when `is_open` is false.
- **`gate_output(kind, **params)`** is the single safety choke point (below).

There is no on-disk session state; OS handle cleanup on process death is
sufficient. DWF access is single-threaded.

## Device profiles & configuration (`devices/profiles.py`, `devices/configs.py`)

The server supports the classic Analog Discovery family. `resolve_profile(devid)`
maps the device-type id (**2 = AD1, 3 = AD2, 10 = AD3**) to a `DeviceProfile`
(supported instruments, fixed-supply voltages for the AD1's non-programmable
rails, etc.); the pin inventory is then refined from the live capability query at
open. This keeps instrument code device-agnostic.

WaveForms devices expose several hardware **configurations** that partition the
FPGA's block RAM differently — a big DigitalIn record buffer at the cost of
smaller output buffers, and so on. On the shared-IO AD1/AD2 these are real
tradeoffs; the independent-IO AD3 can max everything in its default config. The
configuration is fixed at *open*, so the caller declares **intent** via a
`device_config` strategy and `resolve_config_index` picks the concrete index from
that device's config table — `max_digital_in` is config 7 on an AD2 but 3 on an
AD3, and the caller shouldn't have to know that.

| Strategy | Picks | Use when |
|----------|-------|----------|
| `default` (or omitted) | SDK default (returns `None`, doesn't force an index) | Self-stimulus tests (AWG+scope, pattern+logic). A balanced config is required — a max-*input* strategy shrinks the matching **output** buffer on shared-IO devices and starves the source. |
| `max_digital_in` | largest DigitalIn buffer (tie-broken by AnalogIn) | High-rate logic/sniff capture, to avoid DigitalIn overflow on the small-buffer AD1/AD2 (e.g. drove a sniff `lost_samples` 4096-overflow → 0). |
| `max_analog_in` | largest AnalogIn buffer (mirror image) | Long analog-only record. |

`DWF_DEVICE_CONFIG` is a raw-index override (env), device-specific — an escape
hatch for diagnostics, not a normal path. It bypasses strategy resolution
entirely, so an index a device lacks will fail the open.

## Safety model (`policy.py` + `device.gate_output`)

A `SafetyPolicy` is latched at `waveforms.open` and is immutable until close —
changing limits requires close + reopen, with no escape hatch. Fields:

| Field | Bounds |
|-------|--------|
| `supply_max_voltage_pos` / `supply_max_voltage_neg` | programmable rails |
| `supply_max_current` | rail current limit |
| `awg_max_amplitude` | AWG output amplitude |
| `pattern_voltage` | fixed-3.3 V DIO/pattern rail |
| `require_explicit_enable` | outputs never auto-energize |

**Every operation that can make an output go live routes through
`device.gate_output(kind, ...)`**, which checks the policy, raises
`SafetyViolation` on rejection, and appends a JSON line to
`<workspace>/dwf-safety.log` (accepted *and* rejected). Current gate kinds:
`supply_enable`, `awg_start`, `pattern_start`, `dio_set`.

Two subtleties worth knowing when adding output paths:

- **Staging vs. energizing.** `supply.set` stages a setpoint without gating while
  the rail is *disabled* (the gate fires at `enable`). But changing the setpoint
  of an *already-enabled* rail writes live hardware, so that path gates too. The
  same pattern applies to `awg.configure`/`upload_custom` on a running channel.
- **The invariant is "live output ⇒ gated."** If you add a method that enables or
  changes a live output, it must call `gate_output` before touching hardware.

## Pin allocation (`allocator.py`)

`PinAllocator` enforces mutual exclusion on physical DIO/analog pins **and** on
virtual resources (`digital_in`, `i2c_engine`, `uart_engine`, the W1/W2 clock
domain, …). Instruments `claim(instrument_name, [pins])` at configure time;
overlapping claims raise `PinAllocationError` *before* any DWF call. AD3 hardware
constraints (shared clock domains, co-sampled analog channels) are encoded as
`AD3_RESOURCE_GROUPS` that the allocator also checks.

`claim_observe(...)` is the key to concurrent master + sniff: a passive observer
(e.g. `sniff.spi_start`) can share the same wires as an exclusive writer (e.g.
`i2c.configure`) without conflict, because it claims the pins in observe mode
rather than exclusively. Claims for the same instrument key are *replaced* (not
stacked), which is why instruments that own a singular engine (e.g. `logic`
record) carry their own "already running" guard.

## Streaming / record mode (`streaming.py`)

Scope and Logic share record-mode infrastructure for long captures that exceed a
single buffer:

- `RecordingSession` bundles the async poll task, an optional notification task,
  the chunk queue, accumulated chunks, and lost-sample counters.
- `record_loop` polls the backend, reads available samples, runs `process_chunk`
  (which appends to `session.chunks` or writes through to a sync consumer like a
  VCD writer), and enqueues the chunk for live notification.
- `notification_loop` drains the queue to the caller's async `on_chunk` callback.
- `record_stop` cancels the loops, drains the final hardware samples, and calls
  `flush_pending_notifications` so a live subscriber receives the tail of the
  capture (not just the artifact file).

## Observe-mode sniffing (`instruments/sniff.py` + `_async_sniff.py`)

Sniff tools (`spi`, `i2c`, `uart`, `can`) run a DigitalIn record in the
background and decode either after the fact (accumulation) or per-chunk
(`stream_decode: true`, which avoids holding the whole capture in RAM).

- **Memory cap.** `check_memory_cap` rejects a capture projected to exceed
  `MAX_RAW_BYTES` (32 MB). Record mode always stores the full 16-bit digital bank
  (`BYTES_PER_SAMPLE = 16`, one `uint8` per channel per sample), so the bound is
  width-independent — it does **not** scale with the number of decoded pins.
- **Session reaping.** A session that auto-completes but is never `*_stop`'d would
  otherwise leak its allocator claim. `reap_completed_sessions` releases such
  sessions after `SNIFF_REAP_AFTER_S` (300 s); it runs from `Sniff.tick_idle`,
  which the server calls on *every* tool dispatch, so cleanup happens even if the
  client switches to unrelated tools.

## Protocol decoders (`instruments/decoder/`)

Pure-software decoders for SPI/I2C/UART/CAN behind a `Decoder` ABC, with
per-protocol dataclasses for transactions. They run both as the engine behind
observe-mode sniff and as standalone `decoder.*` tools over an existing logic
capture, so the same decode logic serves live and post-hoc workflows.

## Artifacts (`artifacts.py`)

Captures are written to disk; tool results return **paths plus a summary, never
raw samples inline**. Formats: `.npz` (raw samples), `.parquet` (decoded
transactions, queryable with duckdb), optional `.vcd` (PulseView/GTKWave), and a
`.json` sidecar (full config + safety-policy snapshot + pin allocation +
summary) written alongside every capture.

## Backend contract (`backend.py`, `backends/`)

`DwfBackend` is the ABC both backends implement. `FakeBackend` is an in-memory
stand-in for unit tests with canned-data and status-sequence setters;
`PydwfBackend` talks to real hardware via `pydwf`. When adding a backend method,
keep the two in contract — a fake that diverges from real hardware lets tests
pass while hardware misbehaves (see the SPI `transfer_type`/chip-select handling
for a worked example of why the wire-level contract matters).

`is_open` reflects whether a handle is held, not live USB presence — see
[Device loss](troubleshooting.md#device-loss--unplug) for why a physical unplug
is *not* detected by a passive probe and what happens instead.
