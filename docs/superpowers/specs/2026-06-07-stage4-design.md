# dwf-mcp Stage 4 Design: Protocol Sniffing and Decoding

**Date:** 2026-06-07
**Status:** Validated through brainstorming; ready for implementation planning.

---

## Overview

Stage 4 adds passive protocol capture and decoding. Two new instruments are introduced: **`sniff`** (one-shot hardware-backed capture + decode) and **`decoder`** (post-process an existing npz artifact). A `Decoder` ABC is designed for future sigrokdecode compatibility.

Plot/image tools are explicitly out of scope — the LLM can generate visualization code from the existing npz + JSON sidecar artifacts.

---

## Hardware Constraints

The AD3 exposes **one protocol engine instance per bus type**: one I2C, one SPI, one UART, one CAN. Consequently:

- A single dwf-mcp session can have at most one active instrument of each bus type at a time.
- The sniff instrument and its corresponding active master are **mutually exclusive for I2C and CAN** — they share the same hardware engine.
- **UART**: the UART engine is full-duplex in hardware, but `sniff.uart` resets and reconfigures it in RX-only mode. This is mutually exclusive with `uart.configure` / `uart.write` on the same session — sniff would overwrite the TX configuration. Hardware testing of `sniff.uart` therefore requires an external UART transmitter.
- **SPI sniff** uses DigitalIn (a separate hardware block from the SPI protocol engine), so SPI sniff and an SPI master _could_ coexist at the hardware level. However, the allocator uses exclusive claims by default; see the Allocator Extension section.
- Multi-device support (which would allow two I2C buses, etc.) remains deferred as post-v1.

---

## Architecture

Two new instruments sit in the existing instrument layer:

```
src/dwf_mcp/instruments/
  sniff.py              ← Sniff instrument (4 tools)
  decoder/
    __init__.py         ← Decoder instrument (decoder.spi tool)
    base.py             ← Decoder ABC + Transaction dataclass
    spi.py              ← SpiDecoder state machine
```

**`sniff`** — one-shot instrument. A single tool call configures the hardware, captures for `duration_s`, decodes, writes a parquet + JSON sidecar artifact, and returns. No session management or caller polling required.

**`decoder`** — post-processing instrument. Operates on an existing npz from a prior `logic.capture` or `logic.record` call. Only `decoder.spi` ships in Stage 4 (SPI is the only protocol without a hardware decode path).

**`Decoder` ABC** (`decoder/base.py`) — defines the interface `decode(samples, pin_map, **config) → list[Transaction]`. `SpiDecoder` implements it. A future `[sigrok]` optional extra adds `SigrokDecoder` wrapping libsigrokdecode behind the same interface, enabling `decoder.sigrok(protocol, capture_path, pin_map, ...)` without changes to the tool layer.

**Modified files:**
- `src/dwf_mcp/artifacts.py` — add `write_parquet()`
- `src/dwf_mcp/backends/pydwf_backend.py` — add `i2c_spy_start`, `i2c_spy_status`, `i2c_spy_stop`, `uart_sniff`, `can_sniff`
- `src/dwf_mcp/backends/fake.py` — stubs for the above
- `src/dwf_mcp/backend.py` — ABC methods for new backend operations
- `src/dwf_mcp/allocator.py` — DigitalIn observer claim type (see below)
- `src/dwf_mcp/instruments/__init__.py` — register sniff + decoder

---

## Tool Surface

### sniff tools

`sniff.i2c`, `sniff.uart`, and `sniff.can` are single async calls that block for `duration_s` — their hardware engines buffer frames internally, so no concurrent stimulus is needed.

`sniff.spi` uses DigitalIn record mode (a streaming hardware block) and therefore follows the start/status/stop pattern of `logic.record_*`, allowing the caller to perform SPI transfers while capture is in progress.

```
sniff.i2c(sda_pin, scl_pin, duration_s,
          clock_hz=400000, poll_interval_s=0.010, output_path?)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}

sniff.uart(rx_pin, baud, duration_s,
           data_bits=8, parity="none", stop_bits=1,
           poll_interval_s=0.010, output_path?)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}

sniff.can(rx_pin, bitrate, duration_s,
          poll_interval_s=0.010, output_path?)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}

sniff.spi_start(clk_pin, mosi_pin, miso_pin?, cs_pin?, mode, freq_hz,
                poll_interval_s=0.010, output_path?)
  → {sniff_id}

sniff.spi_status(sniff_id)
  → {samples_received, lost_samples}

sniff.spi_stop(sniff_id)
  → {artifact_path, sidecar_path, count, error_count, artifact_error?, summary}
```

On artifact write failure, all tools return `artifact_path: null` and `artifact_error: <str>` with whatever counts were accumulated — same shape as `logic.record_stop`. `poll_interval_s` defaults to 10ms and is stored in the JSON sidecar. `freq_hz` on `sniff.spi_start` sets the DigitalIn sample rate (rule of thumb: 10× the SPI clock). `clock_hz` on `sniff.i2c` is informational only — stored in the sidecar.

### decoder tools

```
decoder.spi(capture_path, clk_pin, mosi_pin,
            miso_pin?, cs_pin?,
            mode=0, bit_order="msb", word_size=8,
            output_path?)
```

`miso_pin` and `cs_pin` are optional. Decodes CLK+MOSI alone if that is all that was captured. Returns `{artifact_path, sidecar_path, count, error_count, artifact_error?, summary}`.

**Pin and rate resolution**: `decoder.spi` reads the JSON sidecar alongside `capture_path` (same base path, `.json` extension) for:
- `pins` list: ordered pin names → column indices in the npz
- `sample_rate_hz`: required for `timestamp_s` computation

If a requested pin is absent from the sidecar pin list, or if `sample_rate_hz` is missing from the sidecar, the tool returns an error immediately without attempting decode.

`decoder.i2c`, `decoder.uart`, `decoder.can` (software state machines for npz) are **deferred** — the hardware spy paths cover the primary use case.

---

## Hardware Paths

### sniff.i2c — hardware spy

Uses `protocol.i2c.spyStart()` then polls `spyStatus(256)` in a `poll_interval_s` asyncio loop for `duration_s`. Each `spyStatus` call returns `(start, stop, data[], nak)` for the most recent event, or `data=[]` if no new data since the last poll. Claims `sda_pin` + `scl_pin` exclusively.

**Transaction assembly**: The poll loop accumulates `spyStatus` results into complete I2C transactions:
- `start=1` signals the beginning of a new transaction. Discard any incomplete prior state.
- Subsequent polls accumulate `data[]` bytes.
- The first accumulated byte is the address byte: `address = byte >> 1`, `direction = "read" if byte & 0x01 else "write"`, `address_bits = 7` (10-bit addressing deferred — spec note in Out of Scope).
- Remaining bytes (`data[1:]` and bytes from subsequent polls before stop) are the transaction payload.
- `stop=1` closes the transaction. Write the completed `Transaction` to the result list.
- A repeated START (`start=1` before `stop=1`) closes the current transaction and begins a new one.
- `nak` value: the exact encoding returned by pydwf `spyStatus` must be verified against hardware at implementation time before any formula is applied. Do not assume a specific mapping between the raw `nak` integer and `nak_at_byte` — treat this as an **implementation TODO** and add a comment in the code once the encoding is confirmed empirically.

**Cleanup contract**: `i2c_spy_stop` (calls `protocol.i2c.reset()`) must be called in a `finally` block, regardless of timeout, poll error, or artifact write failure. Allocator release also happens in `finally`.

### sniff.uart — hardware RX-only

New backend method `uart_sniff(rx_pin_idx, baud, data_bits, parity, stop_bits, duration_s, poll_interval_s)`:
- Resets UART engine, configures with `rxSet(rx_idx)` only (no `txSet`), same init sequence as `uart_configure`.
- Polls `uart.rx(256)` in a drain loop until deadline.
- Returns `list[tuple[float, bytes, bool]]` — `(timestamp_s, data, parity_error)`.

**Cleanup contract**: `uart.reset()` must be called in a `finally` block to leave the engine in a clean state for subsequent `uart.configure` calls. Allocator release also in `finally`.

**Mutual exclusion**: `sniff.uart` resets the UART engine, making it incompatible with an active `uart.configure` session on the same device. Hardware testing therefore requires an external UART transmitter.

### sniff.can — hardware RX-only

New backend method `can_sniff(rx_pin_idx, bitrate, duration_s, poll_interval_s)`:
- Resets CAN engine, configures with `rxSet(rx_idx)` only (no `txSet`).
- Drain-loops `can.rx()` until deadline.
- Returns `list[tuple[float, int, bytes, bool, int]]` — `(timestamp_s, frame_id, data, extended, error_count)`.

**Cleanup contract**: `can.reset()` in `finally`. Allocator release in `finally`.

### sniff.spi — DigitalIn record + SpiDecoder

Uses the backend's DigitalIn record path (same hardware block as `logic.record`). Follows the start/status/stop pattern of `logic.record_*` so the caller can interleave SPI transfers while capture is active.

**`sniff.spi_start`**:
1. Claims DigitalIn resource via `claim_observe` (see Allocator Extension).
2. Calls `backend.logic_record_configure(pin_mask, sample_rate_hz, duration=large_sentinel)` + `logic_record_arm()`. Duration is left open-ended; `sniff.spi_stop` is the explicit terminator.
3. Starts a background `record_loop` task (same as `logic.record_start`) accumulating chunks into a `RecordingSession`.
4. Returns `{sniff_id}`.

**Rollback on start failure**: if step 2 or 3 raises, `backend.logic_record_stop()` is called (best-effort) and the allocator claim is released before re-raising. The caller never receives a `sniff_id` for a session that is not fully armed.

**`sniff.spi_status`**: Returns `{samples_received: N, lost_samples: M}` directly from the `RecordingSession` counters. No decode is performed; word count is not available until `sniff.spi_stop`.

**`sniff.spi_stop`**:
1. Cancels the background `record_loop` task.
2. Calls `backend.logic_record_stop()` to disarm DigitalIn.
3. **Drains remaining available samples**: polls `backend.logic_record_status()` once after stop and reads any remaining `available` samples, appending them to the session chunks. Mirrors `logic.record_stop` drain contract.
4. Concatenates all chunks (including drain), passes to `SpiDecoder` with `sample_rate_hz`.
5. Writes parquet + JSON sidecar.
6. Releases allocator claim.
7. Returns `{artifact_path, sidecar_path, count, error_count, artifact_error?, summary}`.

No npz artifact is written by default (decoded directly from in-memory chunks).

**Cleanup contract**: steps 2, 3, and 6 run in `finally` regardless of decode or artifact write failure. If parquet write fails, returns `artifact_path: null, artifact_error: <str>` with accumulated sample counts.

---

## SpiDecoder

State machine in `decoder/spi.py` implementing the `Decoder` ABC.

**Inputs:** numpy array of shape `(n_samples, 16)` with uint8 0/1 values per bit; `pin_map` dict mapping `"clk"`, `"mosi"`, `"miso"` (optional), `"cs"` (optional) to column indices; `sample_rate_hz` (required, for `timestamp_s` computation); mode (CPOL/CPHA 0–3), bit_order, word_size.

**Logic:**
- Detects CLK edge (rising or falling, per CPOL/CPHA) to sample MOSI/MISO.
- Accumulates `word_size` bits into a word, MSB or LSB first.
- Uses CS assertion/deassertion to group words into transfers.
- Emits a `Transaction` per word with `word_index`, `mosi`, `miso` (null if no MISO), `cs_active`, `cs_error` (CS deasserted mid-word).

**Missing pins:** if `cs_pin` is absent, `cs_active=True` and `cs_error=False` throughout. If `miso_pin` is absent, `miso=None`.

---

## Decoder ABC

```python
# decoder/base.py

class Decoder(ABC):
    protocol_name: ClassVar[str]

    @abstractmethod
    def decode(
        self,
        samples: np.ndarray,       # (n_samples, 16) uint8
        pin_map: dict[str, int],   # signal name → column index
        **config: Any,
    ) -> list[Transaction]:
        ...

@dataclass
class Transaction:
    timestamp_s: float
    # Protocol-specific fields — see Data Model below.
    # All optional fields default to None for hardware-path compatibility.
    ...
```

A future `SigrokDecoder(Decoder)` wraps libsigrokdecode. It lives in an optional `[sigrok]` extra and registers itself in a `DecoderRegistry`. The `decoder.sigrok(protocol, ...)` MCP tool dispatches through the registry. The tool layer never changes.

---

## Data Model

### Parquet schema

One row per decoded unit. Nullable fields are populated by the hardware path where available; sigrokdecode populates all fields.

**Common columns (all protocols):**
| Column | Type | Notes |
|--------|------|-------|
| `timestamp_s` | float64 | Wall-clock offset from capture start |
| `error` | bool | True if any error on this frame |
| `error_detail` | string, nullable | Human-readable error description |

**I2C:**
| Column | Type | Notes |
|--------|------|-------|
| `type` | string | `write` or `read` |
| `address` | int16 | 7-bit or 10-bit |
| `address_bits` | int8 | 7 or 10 |
| `data` | binary | All data bytes in the transaction |
| `nak_at_byte` | int16, nullable | Index of NAKed byte; null = all ACKed |

**SPI:**
| Column | Type | Notes |
|--------|------|-------|
| `word_index` | int32 | Sequential index within capture |
| `mosi` | binary | |
| `miso` | binary, nullable | Null if no MISO pin captured |
| `cs_active` | bool | CS was asserted for this word |
| `cs_error` | bool | CS deasserted mid-word |

**UART:**
| Column | Type | Notes |
|--------|------|-------|
| `data` | binary | |
| `parity_error` | bool | |
| `framing_error` | bool, nullable | Null from hardware path; set by sigrokdecode |
| `break_condition` | bool, nullable | Null from hardware path |

**CAN:**
| Column | Type | Notes |
|--------|------|-------|
| `frame_id` | int32 | |
| `extended` | bool | 29-bit vs 11-bit ID |
| `rtr` | bool | Remote transmission request |
| `dlc` | int8 | Data length code as transmitted |
| `data` | binary | |
| `crc_valid` | bool, nullable | Null from hardware path |
| `ack_received` | bool, nullable | Null from hardware path |
| `error_type` | string, nullable | `invalid_crc`, `ack_error`, `stuff_error`, `form_error`, `bit_error` |

### JSON sidecar

Config snapshot written alongside every parquet: pin assignments, protocol params (baud/bitrate/mode/etc.), `poll_interval_s`, `duration_s`, `sample_rate_hz` (SPI only), `frame_count`, `error_count`, safety policy snapshot.

### ArtifactWriter change

Add `write_parquet(instrument, records, schema, config, output_path?) → ArtifactResult` alongside existing `write_npz`. Uses pyarrow (already a hard dependency). Returns the same `ArtifactResult` shape.

---

## Allocator Extension

DigitalIn (used by `sniff.spi` and `logic.*`) and DigitalOut (used by `pattern.*`) are separate hardware blocks. DigitalIn observing pins driven by DigitalOut causes no electrical conflict.

Add an **observer claim type** to `allocator.py`:

- `allocator.claim_observe(instrument)` — reserves the DigitalIn hardware block as a read-only observer. No pin list needed: DigitalIn is a single global resource; claiming it prevents any other DigitalIn user (logic.capture, logic.record, another sniff.spi) from running concurrently regardless of which pins they use.
- Observer claims **do not** conflict with write claims on any pin — DigitalOut driving a pin while DigitalIn observes the same pin is valid hardware behaviour.
- Observer claims **do** conflict with other observer claims and with exclusive claims on the DigitalIn resource (e.g. `logic.configure` or `logic.record_start`).
- `sniff.spi` uses `claim_observe`; all other sniff tools use the existing `claim` (exclusive) — but see Protocol Engine Resources below.

This is a narrow extension — it reflects the hardware reality that DigitalIn is read-only and enables the SPI hardware test (SPI master + observer sniff) without external devices.

### Protocol Engine Resources

The hardware constraint (one engine per bus type) is not captured by pin-level claims alone. Two I2C configurations on non-overlapping pins would silently conflict at the hardware level under the current model.

Add named engine resources to `AD3_RESOURCE_GROUPS`:
- `i2c_engine` — exclusive; claimed by `i2c.configure` and `sniff.i2c`
- `uart_engine` — exclusive; claimed by `uart.configure` and `sniff.uart`
- `can_engine` — exclusive; claimed by `can.configure` and `sniff.can`
- `spi_engine` — exclusive; claimed by `spi.configure` (sniff.spi uses `digital_in` instead)

All instruments that configure a protocol engine claim both their pin list AND the corresponding engine resource. This applies to existing active instruments (`i2c.py`, `spi.py`, `uart.py`, `can.py`) as well as the new sniff tools — those instruments require a small update to add the engine resource claim alongside their existing pin claims.

`observe=True` as a user-facing escape hatch on protocol-engine sniff tools (I2C/UART/CAN) remains **deferred** — the protocol engines share state with the active master and cannot safely coexist without hardware-specific validation per protocol.

---

## Testing Strategy

### Unit tests

- **`tests/unit/test_spi_decoder.py`** — golden tests for `SpiDecoder`. Synthetic npz fixtures (mode 0, mode 3, no MISO, CS-deasserted-mid-word) checked into `tests/golden/spi/`. Decoder output compared against expected `Transaction` lists. Core testable surface for Stage 4.
- **`tests/unit/test_sniff.py`** — sniff tools with `FakeBackend`. New stubs: `i2c_spy_start()`, `i2c_spy_status()`, `uart_sniff()`, `can_sniff()`. Verifies correct backend calls, empty-poll handling, transaction assembly, parquet schema shape.
- **`tests/unit/test_artifacts.py`** — extend to cover `write_parquet`: column names, dtypes, nullable columns, sidecar JSON shape.

### Hardware tests

**`test_sniff_spi_hardware.py`** — automated, no external devices:
- Jumperless loops MOSI(DIO1)→MISO(DIO2).
- `spi.configure` claims SPI engine pins (write claim on DIO0/DIO1/DIO2/DIO3).
- `sniff_id = sniff.spi_start(...)` arms DigitalIn via `claim_observe` — no pin conflict.
- `spi.transfer([0xA5, 0x5A])` sends known data (sequential call, no concurrency needed).
- `result = sniff.spi_stop(sniff_id)` decodes capture.
- Verify decoded MOSI words match transmitted bytes and MISO matches MOSI (loopback).
- Repeat the same `spi.transfer` call under `logic.capture` (separate test step, no sniff running) to produce an npz; run `decoder.spi` on that npz and verify identical decoded output.

**`test_sniff_uart_hardware.py`** — stub with `pytest.skip("requires external UART transmitter")`. `sniff.uart` resets the UART engine on entry, making concurrent `uart.write` from the same session impossible. External setup note: USB-UART adapter TX → DIO0.

**`test_sniff_i2c_hardware.py`**, **`test_sniff_can_hardware.py`** — stubs with `pytest.skip("requires external I2C/CAN device")`. Notes describe required external setup.

---

## What Is Explicitly Out of Scope for Stage 4

- `decoder.i2c`, `decoder.uart`, `decoder.can` software state machines for npz — deferred
- `decoder.sigrok` / `SigrokDecoder` / `[sigrok]` extra — deferred
- `observe=True` user-facing flag on protocol-engine sniff tools — deferred pending hardware validation
- Plot/image tools — not needed; LLM generates visualization code from npz + sidecar
- Multi-device support
- CAN FD (flexible data-rate) frames
- 10-bit I2C addressing (schema accommodates it via `address_bits`; hardware spy may or may not surface it — verify at implementation time)
- Passive SPI sniff of active-master traffic without Jumperless loopback (the hardware test uses SPI master + observer claim with MOSI→MISO loopback; arbitrary external SPI device capture is supported but not tested)
