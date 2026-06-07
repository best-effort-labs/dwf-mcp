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

All sniff tools are single async calls that block for `duration_s` and return an artifact result. All accept an optional `output_path`.

```
sniff.i2c(sda_pin, scl_pin, duration_s,
          clock_hz=400000, poll_interval_s=0.010, output_path?)

sniff.spi(clk_pin, mosi_pin, miso_pin?, cs_pin?, mode, freq_hz, duration_s,
          poll_interval_s=0.010, output_path?)

sniff.uart(rx_pin, baud, duration_s,
           data_bits=8, parity="none", stop_bits=1,
           poll_interval_s=0.010, output_path?)

sniff.can(rx_pin, bitrate, duration_s,
          poll_interval_s=0.010, output_path?)
```

All return: `{path, sidecar_path, count, error_count, summary: {first_n: [...]}}`

`poll_interval_s` defaults to 10ms and is stored in the JSON sidecar. Users can lower it (e.g. `0.001`) for dense traffic or high baud rates. Each poll cycle drains all available data before sleeping (drain-until-empty pattern), consistent with `record_loop` in `streaming.py`.

`freq_hz` on `sniff.spi` sets the DigitalIn sample rate (rule of thumb: 10× the SPI clock). `clock_hz` on `sniff.i2c` is informational only — the hardware spy requires no clock hint — but is stored in the sidecar.

### decoder tools

```
decoder.spi(capture_path, clk_pin, mosi_pin,
            miso_pin?, cs_pin?,
            mode=0, bit_order="msb", word_size=8,
            output_path?)
```

`miso_pin` and `cs_pin` are optional. Decodes CLK+MOSI alone if that is all that was captured. Returns the same `{path, sidecar_path, count, error_count, summary}` shape.

**Pin resolution**: `decoder.spi` reads the JSON sidecar alongside `capture_path` (same base path, `.json` extension) to get the ordered pin list captured by `logic.*`. It maps the requested pin name strings (`clk_pin`, `mosi_pin`, etc.) to column indices in the npz using that list. If a requested pin was not captured, the tool returns an error immediately without attempting decode.

`decoder.i2c`, `decoder.uart`, `decoder.can` (software state machines for npz) are **deferred** — the hardware spy paths cover the primary use case.

---

## Hardware Paths

### sniff.i2c — hardware spy

Uses `protocol.i2c.spyStart()` then polls `spyStatus(256)` in a `poll_interval_s` asyncio loop for `duration_s`. Each `spyStatus` call returns one decoded transaction `(start, stop, data[], nak)` or empty data if no new transaction. Claims `sda_pin` + `scl_pin` exclusively.

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

Uses the backend's DigitalIn record path (same hardware block as `logic.record`). Internally:
1. Claims DigitalIn resource as observer via `claim_observe` (see Allocator Extension below).
2. Calls `backend.logic_record_configure(pin_mask, sample_rate_hz, duration_s)` + `logic_record_arm()`.
3. Runs a local collect loop (accumulates numpy chunks, no session/notification machinery needed — single blocking call).
4. Passes the raw sample array + `sample_rate_hz` to `SpiDecoder`.
5. Writes parquet + JSON sidecar.

No npz artifact is written by default (decoded directly from the in-memory array).

**Cleanup contract**: `backend.logic_record_stop()` in `finally`. If the parquet write fails, return `{artifact_path: null, artifact_error: <str>, count: N, error_count: M}` — same shape as `logic.record_stop`. Allocator release in `finally`.

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
- `sniff.spi` uses `claim_observe`; all other sniff tools use the existing `claim` (exclusive) on their protocol engine pins.

This is a narrow extension — it reflects the hardware reality that DigitalIn is read-only and enables the SPI and UART loopback hardware tests without external devices.

`observe=True` as a user-facing escape hatch on protocol-engine sniff tools (I2C/UART/CAN) remains **deferred** — the protocol engines share state with the active master and cannot safely coexist without hardware-specific validation per protocol.

---

## Testing Strategy

### Unit tests

- **`tests/unit/test_spi_decoder.py`** — golden tests for `SpiDecoder`. Synthetic npz fixtures (mode 0, mode 3, no MISO, CS-deasserted-mid-word) checked into `tests/golden/spi/`. Decoder output compared against expected `Transaction` lists. Core testable surface for Stage 4.
- **`tests/unit/test_sniff.py`** — sniff tools with `FakeBackend`. New stubs: `i2c_spy_start()`, `i2c_spy_status()`, `uart_sniff()`, `can_sniff()`. Verifies correct backend calls, empty-poll handling, transaction assembly, parquet schema shape.
- **`tests/unit/test_artifacts.py`** — extend to cover `write_parquet`: column names, dtypes, nullable columns, sidecar JSON shape.

### Hardware tests

**`test_sniff_spi_hardware.py`** — automated, no external devices:
- Jumperless loops MOSI(DIO1)→MISO(DIO2) and CS(DIO3)→DIO3 (self — CS is driven by the SPI master).
- `spi.configure` claims SPI engine pins (write claim on DIO0/DIO1/DIO2/DIO3).
- `sniff.spi` runs concurrently using `claim_observe` on DigitalIn — no pin conflict (DigitalIn observer + DigitalOut/protocol-engine write on same pins is allowed).
- `spi.transfer([0xA5, 0x5A])` sends known data; `sniff.spi` decodes the capture.
- Verify decoded MOSI words match transmitted bytes and MISO matches MOSI (loopback).
- Save npz from capture, run `decoder.spi` on it; verify identical output.

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
- Passive SPI sniff of existing active-master traffic without external Jumperless loopback (requires allocator observer claim, which is in scope, but the SPI master + sniff coexistence is a side effect — primary test path is Pattern + sniff)
