# Stage 5: Software Protocol Decoders + Concurrent Master/Sniff

## Overview

Stage 5 adds **software state-machine decoders** for I2C, UART, and CAN that operate on raw `DigitalIn` captures (**npz** artifacts written by `logic.record_stop`), and adds new async observe-mode sniff tools (`sniff.{i2c,uart,can}_start/status/stop`) that capture via `DigitalIn` and run those same decoders. The blocking `sniff.{i2c,uart,can}` engine-mode tools are unchanged; observe-mode is opt-in via the new start tools. All paths write **parquet** matching the existing `sniff.{i2c,uart,can}` output shape, so consumers can't tell which path produced the result.

The motivating constraint: the AD3 has exactly **one** I2C / UART / CAN protocol engine in hardware. Today, `sniff.i2c` uses the I2C engine's `spyStart` and therefore cannot run concurrently with `i2c.scan` / `i2c.write` / `i2c.read` — they conflict via the `i2c_engine` virtual claim. The `DigitalIn` logic-analyzer block is a separate hardware unit, so capturing protocol traffic on DIO pins while the protocol-engine master is busy is physically possible if we decode in software. `sniff.spi` already does this; this stage extends the same pattern to I2C/UART/CAN.

Plot/image tools remain out of scope (LLM generates visualization code from artifacts).

---

## New Tool Surface

```
decoder.i2c(capture_path, sda_pin, scl_pin, output_path?)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}

decoder.uart(capture_path, rx_pin, baud, data_bits=8, parity="none",
             stop_bits=1, polarity=0, output_path?)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}

decoder.can(capture_path, rx_pin, bitrate, output_path?)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}
```

Each tool reads the **npz** written by `logic.record_stop` plus its JSON sidecar (for `pins`, `sample_rate_hz`), runs a state-machine decoder over the raw samples, and writes a **parquet** of decoded transactions/frames matching the schema already used by `sniff.{i2c,uart,can}`.

## New Async Sniff Tools — `sniff.{i2c,uart,can}_start/status/stop`

The existing **blocking** `sniff.{i2c,uart,can}(duration_s=...)` tools are unchanged. They keep their current behavior (protocol-engine spy, mutual exclusion with master mode).

Stage 5 adds **async observe-mode** counterparts that mirror the existing `sniff.spi_start/status/stop` lifecycle:

```
sniff.i2c_start(sda_pin, scl_pin, clock_hz, max_duration_s,
                sample_rate_hz?, output_path?)
  → {sniff_id}

sniff.i2c_status(sniff_id)
  → {samples_received, lost_samples, done}

sniff.i2c_stop(sniff_id)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}
```

`max_duration_s` is **required** and bounds the worst-case capture-buffer footprint. The capture auto-stops after `max_duration_s` even if `*_stop` is never called. `*_status` returns `done=True` once the capture has self-stopped.

### Orphan-session reaping

Auto-stop releases the *hardware* (DigitalIn block goes idle) but the allocator claim and chunk buffers remain held until either `*_stop` is called or the session is reaped. Without reaping, a caller that crashes or forgets `*_stop` after auto-stop leaks the `claim_observe` until `waveforms.close`, blocking other observers.

Reaping rule: on every `*_start` and `*_status` call, the Sniff instrument runs a sweep that releases any auto-stopped session whose `completed_at` is older than `SNIFF_REAP_AFTER_S` (default 300 s — 5 minutes). Reaping:

1. Cancels the (already-completed) background task.
2. Calls `backend.logic_record_stop()` for idempotency (cheap if already stopped).
3. Releases the `claim_observe`.
4. Drops the session from `_spi_sessions` / `_async_sniff_sessions`.

After a session has been reaped, calling `*_status` or `*_stop` with its `sniff_id` returns the standard "unknown sniff_id" `ValueError`. The reap timestamp is logged at WARNING level so silent data loss doesn't go unnoticed.

`*_stop` called within the retention window works normally: drain, decode, write artifact, release.

Same three-tool shape for `uart` and `can`. The start tool returns immediately; capture runs in a background `record_loop` task; stop terminates capture, decodes via the software decoder, and writes a parquet matching the existing `sniff.{i2c,uart,can}` row schema.

**Why the start/status/stop split rather than a flag on the blocking tool:** the MCP request/response model has no "streaming" concept, so async work must be modeled as start/poll/stop. A blocking `sniff.i2c(observe=True, duration_s=T)` would either freeze the event loop for `T` seconds (blocking concurrent `i2c.scan` calls) or quietly spawn background tasks the caller can't reason about. The start/status/stop pattern is already proven for `sniff.spi`.

**Why no `observe` flag at all:** the two paths have different lifecycle models. Conflating them under one tool name leaks the choice into every caller. Distinct tools make the trade-off explicit at the call site.

### Concurrency with master mode

Observe-mode sniff calls `device.allocator.claim_observe("sniff_<protocol>_<sniff_id>")` — that's the **only** claim it makes. It does NOT claim `<protocol>_engine` and does NOT claim the observed physical DIO pins. This is what enables `sniff.i2c_start(sda_pin="dio0", scl_pin="dio1")` to run concurrently with `i2c.configure(sda_pin="dio0", scl_pin="dio1")` + `i2c.scan()` on the same wires.

The observed pins are read-only consumers of the DigitalIn block; conflicts with output writers (`dio.set`, `pattern.*`) on the same pins are physically meaningful and remain caller's responsibility — but our test fixtures verify the common case (master on same pins).

---

## Code Layout

```
src/dwf_mcp/instruments/decoder/
  __init__.py          # Decoder instrument (tool dispatch + artifact I/O)
  spi.py               # existing SpiDecoder
  i2c.py               # NEW — I2cDecoder state machine
  uart.py              # NEW — UartDecoder state machine
  can.py               # NEW — CanDecoder state machine
  _common.py           # NEW — shared helpers (edge detection, sample → time mapping)
```

`Decoder` ABC (already exists at `decoder/base.py`) is unchanged. Each new decoder implements:

```python
class XDecoder:
    def decode(
        self,
        samples: np.ndarray,        # shape (N, D) — N samples × D channels (uint8 0/1)
        pin_map: dict[str, int],    # protocol-specific pin names → DIO column index
        sample_rate_hz: float,
        **protocol_params,          # baud, bitrate, polarity, etc.
    ) -> list[Transaction]: ...
```

`Transaction` is the existing dataclass; each decoder picks the fields that apply (e.g., `address` for I2C, `frame_id` for CAN, just `data` for UART).

`sniff.py` grows three new tool methods (`i2c_start`/`i2c_status`/`i2c_stop` and analogues for uart/can) that mirror the existing `spi_start`/`spi_status`/`spi_stop` implementation. They:

1. Call `backend.logic_record_configure/arm` **directly** (NOT the public `Logic` instrument — that would claim physical DIO pins and conflict with the master we're trying to coexist with).
2. Spin up `record_loop` as a background asyncio task. The backend's `logic_record_configure` is given `duration_s=max_duration_s` so hardware self-stops at the cap.
3. On `stop`, cancel the task, drain remaining samples via `backend.logic_record_status` + `read`, run the corresponding decoder, write a parquet sidecar, release the `claim_observe`.

The pattern is identical to `sniff.spi_start/stop` (today's only async observe path). The three new protocols just plug in different decoders. Refactor `sniff.spi_start/stop`'s plumbing into a shared `_AsyncSniffSession` helper to avoid four near-duplicate copies.

### Start failure cleanup contract

`*_start` may fail at any of: `claim_observe`, `logic_record_configure`, `logic_record_arm`, `RecordingSession` construction, or `asyncio.create_task`. Every path that has acquired a resource must roll it back before raising:

```
claim_observe       → must release on any later failure
logic_record_configure → must call logic_record_stop on any later failure
logic_record_arm    → must call logic_record_stop on any later failure
task creation fail  → must call logic_record_stop AND release claim
```

The implementation uses a single `try` around steps 2-3 with a `except: cleanup; raise` that calls `logic_record_stop()` (under `try/except log.warning`) and `allocator.release(allocator_key)`. The existing `sniff.spi_start` (lines 354-358) is the template. The shared `_AsyncSniffSession` helper centralizes this so the three new protocols can't drift.

---

## Decoder State Machines

### I2cDecoder

Inputs: `pin_map = {"sda": int, "scl": int}`. Walk the samples looking for SCL/SDA edges with the standard rules:

- **START:** SDA falling while SCL high.
- **STOP:** SDA rising while SCL high.
- **Bit sample:** SDA value at SCL rising edge → MSB-first byte assembly, every 9th bit is ACK/NAK (drives `nak_at_byte`).

Output: list of transactions matching the schema in `_close_i2c_transaction` (`type`, `address`, `address_bits=7`, `data`, `nak_at_byte`, `error`). 10-bit addressing remains out of scope (same as Stage 4).

Minimum sample rate guidance: 10× the I2C clock (so 100 kHz I2C → 1 MHz capture).

### UartDecoder

Inputs: `pin_map = {"rx": int}` + `baud`, `data_bits`, `parity`, `stop_bits`, `polarity`. State machine:

- Wait for start-bit edge (fall if `polarity=0`, rise if `polarity=1`).
- Sample at mid-bit positions computed from `sample_rate_hz / baud` (round to nearest integer; require ≥ 4× oversampling or refuse).
- Assemble byte LSB-first; check parity; verify stop bit(s).

Output: list of `{timestamp_s, data, parity_error, framing_error, break_condition, error, error_detail}` matching the existing schema produced by `sniff.uart`.

### CanDecoder

Inputs: `pin_map = {"rx": int}` + `bitrate`. Capture-rate guidance: 10× bitrate (the absolute minimum; 20× is preferred — see sampling note below).

**Bus polarity:** CAN convention is **dominant = 0** (active drive), **recessive = 1** (idle). The decoder assumes this convention directly on `rx`; users with inverted transceivers must invert upstream.

**Sample point:** CAN samples each bit at ~75-87.5% of the bit time (the "second sample point" / SJW boundary), NOT the midpoint. Implementation:

- Compute samples-per-bit as `round(sample_rate_hz / bitrate)`. Require ≥ 8 (refuse otherwise — at lower oversampling the sample-point error exceeds bit-time tolerance).
- Sample each bit at index `int(samples_per_bit * 0.75)` from the bit's start. This matches the CiA 75% recommendation and is robust at 10×-20× oversampling.

State machine handles Standard frames only (no FD, no 29-bit IDs for v1 — both in Out of Scope):

- Detect SOF: first dominant edge after ≥ 11 recessive bit-times (or first capture edge).
- Bit-stuffing decode: after 5 same-value bits, drop the next bit.
- Parse arbitration field (11-bit ID + RTR), control (IDE=0 dominant + r0 + 4-bit DLC), data (0-8 bytes), 15-bit CRC + 1 CRC-delimiter, ACK slot + ACK-delimiter, 7-bit EOF.
- Compute CRC over arbitration + control + data using the CAN CRC-15 polynomial (`0x4599`) and compare against the captured CRC. Set `crc_valid=False` on mismatch.
- ACK slot is dominant if a receiver acknowledged. With a passive-observer setup and no other node on the bus, expect `ack_received=False`.

**Output row schema** (must exactly match Stage 4 `sniff.can`'s parquet rows so consumers can't tell which path produced the artifact):

```python
{
    "timestamp_s": float,
    "frame_id": int,
    "extended": False,            # v1: 11-bit IDs only
    "rtr": bool,
    "dlc": int,
    "data": bytes,
    "crc_valid": bool | None,     # None = couldn't compute (truncated frame)
    "ack_received": bool | None,
    "error_type": str | None,     # "form" | "crc" | "stuff" | "bit" | None
    "error": bool,
    "error_detail": str | None,
}
```

The **tool response** (from `sniff.can_stop` and `decoder.can`) is the standard `{artifact_path, sidecar_path, count, error_count, artifact_error?, summary}` shape used by all decoder/sniff tools — `count` = number of rows, `error_count` = rows where `error=True`.

---

## Allocator Behavior

No new resource groups. Behavior precisely:

- Async observe-mode sniff (`sniff.{i2c,uart,can}_start`) calls **only** `device.allocator.claim_observe("sniff_<protocol>_<sniff_id>")`. It does NOT claim the `<protocol>_engine` virtual pin and does NOT claim the physical DIO pins it samples. This is what allows it to coexist with a protocol master on the same wires.
- Blocking engine-mode sniff (existing `sniff.{i2c,uart,can}`) is unchanged: it claims `<protocol>_engine` plus the configured DIO pins. Mutually exclusive with master mode, as today.
- The `claim_observe` mechanism enforces "only one DigitalIn observer at a time" and "no observer while DigitalIn is exclusively claimed by `logic.*`". That covers the only physical conflict that matters: two consumers of the single DigitalIn block.

### Required allocator tests

Unit tests must prove:

1. `claim_observe("sniff_i2c_X")` succeeds while `claim("i2c", ["i2c_engine", "dio0", "dio1"])` is active. (The two coexist.)
2. `claim_observe("sniff_i2c_X")` while another observer (`sniff_spi_Y`) holds DigitalIn raises `PinAllocationError`.
3. `claim("logic", ["digital_in", ...])` while `sniff_i2c_X` is observing raises `PinAllocationError`.

(1) is the key new invariant; (2) and (3) verify the existing `claim_observe` invariants still hold.

---

## Schemas

### `decoder.*` schemas (post-process)

```python
DECODER_I2C_SCHEMA = {
    "type": "object",
    "required": ["capture_path", "sda_pin", "scl_pin"],
    "properties": {
        "capture_path": {"type": "string"},
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "output_path": {"type": "string"},
    },
}

DECODER_UART_SCHEMA = {
    "type": "object",
    "required": ["capture_path", "rx_pin", "baud"],
    "properties": {
        "capture_path": {"type": "string"},
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "polarity": {"type": "integer", "enum": [0, 1], "default": 0},
        "output_path": {"type": "string"},
    },
}

DECODER_CAN_SCHEMA = {
    "type": "object",
    "required": ["capture_path", "rx_pin", "bitrate"],
    "properties": {
        "capture_path": {"type": "string"},
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "output_path": {"type": "string"},
    },
}
```

### `sniff.*_start` schemas (async observe-mode)

Each start tool takes the same protocol params as its blocking counterpart **plus** required `max_duration_s` (memory cap + auto-stop) and an optional `sample_rate_hz` (overrides the per-protocol default). Without override, sample rate defaults to `10 × clock_hz` (I2C), `10 × baud` (UART), or `20 × bitrate` (CAN — needed for the 75% sample point per the CAN section).

```python
SNIFF_I2C_START_SCHEMA = {
    "type": "object",
    "required": ["sda_pin", "scl_pin", "clock_hz", "max_duration_s"],
    "properties": {
        "sda_pin": {"type": "string", "pattern": _PIN_RE},
        "scl_pin": {"type": "string", "pattern": _PIN_RE},
        "clock_hz": {"type": "integer", "minimum": 1_000},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
        "sample_rate_hz": {"type": "number", "minimum": 1_000.0},  # optional override
        "output_path": {"type": "string"},
    },
}

SNIFF_UART_START_SCHEMA = {
    "type": "object",
    "required": ["rx_pin", "baud", "max_duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "baud": {"type": "integer", "minimum": 300},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
        "data_bits": {"type": "integer", "enum": [5, 6, 7, 8], "default": 8},
        "parity": {"type": "string", "enum": ["none", "odd", "even"], "default": "none"},
        "stop_bits": {"type": "integer", "enum": [1, 2], "default": 1},
        "polarity": {"type": "integer", "enum": [0, 1], "default": 0},
        "sample_rate_hz": {"type": "number", "minimum": 1_000.0},
        "output_path": {"type": "string"},
    },
}

SNIFF_CAN_START_SCHEMA = {
    "type": "object",
    "required": ["rx_pin", "bitrate", "max_duration_s"],
    "properties": {
        "rx_pin": {"type": "string", "pattern": _PIN_RE},
        "bitrate": {"type": "integer", "minimum": 10_000},
        "max_duration_s": {"type": "number", "minimum": 0.001, "maximum": 3600.0},
        "sample_rate_hz": {"type": "number", "minimum": 1_000.0},
        "output_path": {"type": "string"},
    },
}
```

`SNIFF_I2C_STATUS_SCHEMA` / `STOP_SCHEMA` (and uart/can analogues) follow the existing `SPI_STATUS_SCHEMA` / `SPI_STOP_SCHEMA` input shape: required `{"sniff_id": string}`.

**Status return shape:** `{samples_received, lost_samples, done}`. The new `done` field reports whether the capture has self-stopped (because `max_duration_s` elapsed or the backend signalled `DwfState.Done`). It is added to `sniff.spi_status` as well — additive, non-breaking, and useful so callers can detect auto-stop without polling for sample-count plateaus.

**Sample-rate caps (enforced at `*_start`):**
- Floor: enforced per-protocol (I2C requires ≥ 4× clock, UART ≥ 4× baud, CAN ≥ 8× bitrate per the CAN section). Below the floor → `ValueError`.
- Ceiling: `sample_rate_hz × max_duration_s × n_pins ≤ 32 MB raw` (uint8 per sample). Above → `ValueError` with a suggested cap.

**Why 32 MB raw (not 100 MB):** `*_stop` concatenates the chunk list into a single contiguous array before handing it to the decoder (`np.concatenate` allocates the full target buffer while the source chunks are still alive). Peak memory during that step is **2× raw** plus the decoded parquet records (typically << raw for the protocols we support: a few hundred bytes per I2C transaction or CAN frame). Bounding raw at 32 MB keeps the worst-case decode peak under ~100 MB. The future streaming/incremental decode noted under "Open Questions" would let us raise this without a peak-memory penalty.

Bounded because `max_duration_s` is required and itself capped at 3600 s.

The existing blocking `sniff.{i2c,uart,can}` schemas are unchanged. No `observe` flag added anywhere.

---

## Sidecar Reading

The decoder tools read `capture_path` + the matching `*.json` sidecar to recover:

- `pins` list and the DIO indices used at capture time
- `sample_rate_hz`
- Original protocol params (echoed back in the decoded sidecar for traceability)

A missing or malformed sidecar is a hard error returned via `artifact_error`. Unit tests cover both well-formed and missing-sidecar cases.

---

## Out of Scope for Stage 5

- CAN FD (flexible data-rate) frames
- CAN extended (29-bit) identifiers — v1 surfaces only standard 11-bit frames
- 10-bit I2C addressing (carried over from Stage 4)
- I2C clock stretching by an external slave during observe-mode capture is fine (we just sample edges), but stretching during master-mode `i2c.write/read` is governed by the engine, not us
- `decoder.sigrok` / libsigrokdecode wrapper — still deferred
- Plot/image tools — still deferred

---

## Testing

### Unit tests (no hardware)

- `tests/unit/test_i2c_decoder.py` — synthetic samples produced by an inverse generator (transaction → samples → decode → assert round-trip). Cover: single write, write-then-read, NAK on address, NAK on data byte, multiple back-to-back transactions, clock stretching.
- `tests/unit/test_uart_decoder.py` — generator for byte sequences at various baud / parity / stop / polarity combinations. Cover: clean bytes, parity error, framing error, break condition.
- `tests/unit/test_can_decoder.py` — generator for standard CAN frames. Cover: simple frame, max-DLC (8 bytes), bit-stuffing required, CRC mismatch.
- `tests/unit/test_decoder.py` already covers the tool-layer dispatch; extend with i2c/uart/can cases that pass a real (small) capture file through and assert the artifact path + count.
- `tests/unit/test_sniff.py` extends to cover the new `sniff.{i2c,uart,can}_start/status/stop` async tools against the fake backend: start succeeds; status reports samples + `done`; stop decodes + releases claim; start-failure rollback releases the claim and stops record mode; `max_duration_s` self-stops (status returns `done=True` after the cap); memory-cap violation at start raises `ValueError` before any hardware call; **session reaping** — after `SNIFF_REAP_AFTER_S` elapses on an auto-stopped session, a subsequent `*_start` or `*_status` call releases the orphan claim and a later `*_stop` with the reaped id returns the standard "unknown sniff_id" error.

### Hardware tests

- `tests/hardware/test_sniff_i2c_observe_hardware.py` — RP2350B drives I2C master, sniff via `sniff.i2c_start/stop` on the same wires, decode, assert addresses. **Plus a coexistence case:** spin up `sniff.i2c_start` against `dio0/dio1`, then call `i2c.configure(sda_pin="dio0", scl_pin="dio1")` + `i2c.scan()` concurrently, assert both succeed and the sniff captures the scan traffic.
- `tests/hardware/test_sniff_uart_observe_hardware.py` — mirror of `test_sniff_uart_hardware.py` using the async observe-mode tools instead of the blocking engine variant.
- `tests/hardware/test_decoder_i2c_post_process.py` — record raw samples via the backend's logic-record path into an npz, then call `decoder.i2c` to decode after the fact. Verifies the post-process path works on real captures.

CAN hardware test remains gated on having an external transceiver (same as Stage 4).

---

## Open Questions / Future Work

- **Streaming/incremental decode.** Stage 5 buffers all chunks until `*_stop` and concatenates before decoding. This caps raw at 32 MB to keep peak memory ≈ 100 MB. A future revision could make each decoder stateful (`feed(chunk)` / `finalize()`), letting `record_loop` decode chunks as they arrive and write to parquet incrementally. That would lift the 32 MB cap to whatever fits on disk and remove the 2× concat spike. Not in Stage 5 because the protocol-engine sniff tools (which Stage 5 doesn't replace) handle long captures fine, and the bounded observe-mode case fits comfortably in 32 MB.
- **Configurable reap window.** `SNIFF_REAP_AFTER_S` is a module constant in Stage 5. If users need per-session retention they can call `*_stop` immediately to bypass; tweaking the default is a one-line change. A `reap_after_s` parameter on `*_start` could be added if a use case appears.
- **CAN sample-point tolerance on noisy captures.** The 75% sample point is the CiA recommendation but real buses may need adjustment for ringing or transceiver delay. If decoded bit-error rates are high on real traffic, expose `sample_point_pct` as an optional tuning parameter.
