# Handoff: Stage 3a+scope.record → Stage 3b

**Date:** 2026-06-05  
**Audience:** A fresh Claude Code session planning and implementing the next phase of dwf-mcp.

This file is self-contained. Read it before doing anything else.

---

## TL;DR

All instrument code (DMM, SPI, UART, CAN, scope.record, logic.record) is implemented and unit-tested. Hardware tests exist but most have **not yet been run against real hardware**. Stage 3b is primarily **hardware test validation + any gaps found during that process**. There is no significant new code to write — the work is verifying what exists, fixing the bugs that hardware tests reveal, and then deciding whether there is a Stage 4.

---

## Project location

- Working directory: `/Users/claude/work/dwf-mcp/dwf-mcp`
- Python: 3.12
- venv: `.venv/` in repo root (gitignored) — `pip install -e ".[dev]"` to recreate
- No worktrees active

## Quick health check

```bash
cd ~/work/dwf-mcp/dwf-mcp
pytest -m 'not hardware' --tb=short -q   # expect: 250 passed, 13 deselected
ruff check src/ tests/                   # expect: All checks passed!
mypy src/                                # expect: Success
```

---

## What is built (complete list)

### Instrument layer — all implemented

| Instrument | Tools | Unit tests | Hardware tests | HW validated? |
|---|---|---|---|---|
| Scope (buffer) | `scope.configure`, `scope.set_trigger`, `scope.capture` | ✅ | `test_scope_hardware.py` | ✅ |
| Scope (record) | `scope.record_start`, `scope.record_status`, `scope.record_stop` | ✅ | `test_scope_record_hardware.py` | ✅ (2026-06-05) |
| AWG | `awg.configure`, `awg.start`, `awg.stop`, `awg.upload_custom` | ✅ | `test_awg_hardware.py` | ✅ |
| Supply | `supply.set`, `supply.enable`, `supply.disable`, `supply.read` | ✅ | `test_supply_hardware.py` | ✅ |
| I2C | `i2c.configure`, `i2c.write`, `i2c.read`, `i2c.write_read`, `i2c.release` | ✅ | `test_i2c_hardware.py` | ✅ |
| Logic (buffer) | `logic.configure`, `logic.set_trigger`, `logic.capture` | ✅ | `test_logic_hardware.py` | ✅ |
| Logic (record) | `logic.record_start`, `logic.record_status`, `logic.record_stop` | ✅ | **none written** | ❌ |
| Pattern | `pattern.configure`, `pattern.start`, `pattern.stop`, `pattern.release` | ✅ | (covered by logic HW test) | ✅ |
| DIO | `dio.set_direction`, `dio.set`, `dio.read` | ✅ | `test_dio_hardware.py` | ✅ |
| DMM | `dmm.measure` | ✅ | `test_dmm_hardware.py` | **NOT RUN** |
| SPI | `spi.configure`, `spi.transfer`, `spi.write`, `spi.read`, `spi.release` | ✅ | `test_spi_hardware.py` | **NOT RUN** |
| UART | `uart.configure`, `uart.write`, `uart.read`, `uart.release` | ✅ | `test_uart_hardware.py` | **NOT RUN** |
| CAN | `can.configure`, `can.send`, `can.receive`, `can.release` | ✅ | `test_can_hardware.py` | **NOT RUN** |

### Backend layer

`PydwfBackend` in `src/dwf_mcp/backends/pydwf_backend.py` has full implementations for all 13 instruments. `FakeBackend` has stubs for all.

### VCD writer

`src/dwf_mcp/vcd_writer.py` has both batch (`write()`) and incremental (`IncrementalVcdWriter`) writers. Used by `logic.capture` (format="vcd") and `logic.record_start` (format="vcd"). Requires `pip install dwf-mcp[vcd]` (pyvcd). Unit-tested in `test_vcd_writer.py`.

### Streaming layer

`src/dwf_mcp/streaming.py` — `record_loop` + `process_chunk` + `notification_loop`. Used by both scope.record and logic.record. **Critical bug was fixed 2026-06-05:** `statusRecord()` third value is `pcdCorrupt` (almost always 0), NOT "remaining samples". The loop now exits by checking `DwfState.Done` from `status(True)`. This applies to both `scope_record_status` and `logic_record_status` in `pydwf_backend.py`.

---

## Bugs fixed today (2026-06-05) — must not re-introduce

### 1. pydwf `statusRecord()` misinterpretation

**File:** `src/dwf_mcp/backends/pydwf_backend.py`

`analog_in.statusRecord()` and `digital_in.statusRecord()` both return `(available, lost, corrupt)` where `corrupt` is almost always 0. The code was treating `corrupt` as "remaining samples" and exiting `record_loop` after the first poll with only ~10ms of data. Fixed by:

```python
def scope_record_status(self) -> tuple[int, int, int]:
    from pydwf.core.auxiliary.enum_types import DwfState
    state = self._analog_in.status(True)
    available, lost, _ = self._analog_in.statusRecord()
    # Third tuple position is corrupt count (always ~0), not remaining samples.
    # Return 0 only when device signals Done so record_loop exits correctly.
    return int(available), int(lost), 0 if state == DwfState.Done else 1
```

Same pattern for `logic_record_status`. **Do not change this logic.** If you see a test that appears to collect only a few samples, this bug has been re-introduced.

### 2. Hardware test ground topology

**Files:** `tests/hardware/test_scope_record_hardware.py`, `tests/hardware/pinout.py`

AD3 scope inputs are true differential — CH1_NEG and CH2_NEG are **not** internally connected to AD3 GND. For single-ended measurements routed through Jumperless, two explicit connections are required:

1. **gnd_bridge**: `AD3_GND` → Jumperless `"GND"` rail. Without this, Jumperless-routed signals have no common reference with the AD3 measurement circuit. USB ground impedance introduces systematic offsets.
2. **chN_neg**: `CHN_NEG` → `AD3_GND`. Without this, the negative input floats and the reading is garbage.

`pinout.py` has `AD3_GND: ("top", 12)` = row 13. Every test that measures an AD3 analog input via Jumperless must include both connections.

---

## Stage 3b work: hardware test validation

Run each hardware test file and fix any failures. Expected test setup:

```bash
# Run one file at a time with verbose output
pytest tests/hardware/test_dmm_hardware.py -v -m hardware
pytest tests/hardware/test_spi_hardware.py -v -m hardware
pytest tests/hardware/test_uart_hardware.py -v -m hardware
pytest tests/hardware/test_can_hardware.py -v -m hardware
```

### DMM hardware test — known issue to fix first

`test_dmm_hardware.py::test_dmm_measures_awg_dc_voltage` connects `W1→CH1_POS` but is **missing the ground bridge and CH1_NEG connection**. This will likely produce wrong readings (same issue that plagued scope.record before today's fix).

Current wiring in the test:
```python
@pytest.mark.jumperless(connections={"awg_to_scope": ("W1", "CH1_POS")})
```

Required wiring (fix before running):
```python
@pytest.mark.jumperless(connections={
    "gnd_bridge": ("AD3_GND", "GND"),
    "ch1_neg": ("CH1_NEG", "AD3_GND"),
    "awg_to_scope": ("W1", "CH1_POS"),
})
```

Also add this comment explaining why (following the pattern in `test_scope_record_hardware.py`).

### SPI hardware test

`test_spi_hardware.py::test_spi_loopback_transfer` uses digital I/O only (no analog connections), so ground topology is not a concern. Loopback: MOSI(DIO1)→MISO(DIO2). CLK=DIO0, CS=DIO3.

Jumperless wiring: `DIO1→DIO2` via conftest.py `wire` fixture (already in the test).

### UART hardware test

`test_uart_hardware.py::test_uart_loopback` uses digital I/O. TX(DIO0)→RX(DIO1) loopback.

**Known pydwf UART nuance:** The UART RX read returns `(data_bytes, parity_error_bool)`. The test checks `parity_error is False` — verify the pydwf backend returns a bool (not an int).

### CAN hardware test

`test_can_hardware.py::test_can_tx_frame_activity` does NOT use the MCP tool layer — it talks to pydwf directly to set up DigitalIn and verify DIO0 transitions during a CAN frame transmission. This is a TX-path smoke test. A full bidirectional CAN test requires an external CAN transceiver; that's out of scope for now.

The test requires no Jumperless wiring (it just reads DIO0 internally). It should run as-is if CAN TX is on DIO0.

### Logic record hardware test — missing, needs writing

There is no `test_logic_record_hardware.py`. Write a test analogous to `test_scope_record_hardware.py::test_scope_record_dc_signal`:

1. Configure Pattern to output a clock on DIO0
2. Start a logic.record_start recording on DIO0 at adequate sample rate
3. Wait for completion via logic.record_status
4. Call logic.record_stop
5. Load the npz artifact and verify transitions (rising + falling edges) are present

Wiring required: `DIO0→DIO1` (pattern output to logic input), or self-loop on DIO0 (AD3 supports capturing its own DIO output — check whether the backend allows this before using it).

---

## Architecture notes — read before writing any code

### Tool dispatch pattern

Instruments declare their MCP tools in a `ClassVar[dict]` on the class. `DwfMcpApp.register_instrument(cls)` iterates `cls.tools` and registers `{cls.name}.{suffix}` → `method_name`. Streaming tools (`record_start`) get special handling in `server.py:116` to wire in `on_record_chunk`.

### Safety gate

All output-enabling operations must go through `device.gate_output(policy_check_args)`. Never call pydwf output functions directly from an instrument without gating. Any new SPI/UART/CAN method that drives a pin should pass through this gate.

### Pin allocation

Call `device.allocator.claim(instrument_name, pin_list)` before any DWF configuration and `release(instrument_name)` in the finally block. Overlapping claims raise `PinAllocationError`. Pin names are like `"dio0"`, `"scope1"`, `"scope2"`, `"analog_out1"`. See `src/dwf_mcp/devices/ad3.py` for the full list.

### Fake backend usage in tests

All unit tests use `FakeBackend`. When adding pydwf backend methods, add corresponding stubs to `FakeBackend` in `src/dwf_mcp/backends/fake.py` with the same signature. The fake records all calls and returns canned responses. See existing stubs for the pattern.

### Jumperless conftest.py wire fixture

`conftest.py::wire` reads the `@pytest.mark.jumperless(connections={...})` marker and connects pairs using `pinout.row(signal_name)`. The 0.3s per-connection sleep is load-bearing — the CH446Q firmware needs time to program each route before the next. Do not remove it.

Connection order within a test's `connections` dict matters when signals share a bus row on the CH446Q crossbar. If two signals conflict, connect the one that is "less dependent" first. See the `test_scope_record_two_channels` comments for the specific W1/W2 bus conflict on chip1 y=4.

---

## Known hardware facts

- **Row 14 (CH2_POS)**: Previously thought to have a permanent 10MΩ fault — this was a transient state cleared by power cycling the Jumperless. `CH2_POS` is back at its natural `("top", 13)` = row 14. No bypass needed.
- **RC settling**: The Jumperless breadboard+CH446Q path adds ~125ms settling time on scope inputs. Use ≥0.5s duration for scope.record hardware tests.
- **CAN transceiver**: Full CAN validation (including ACK handshake) requires an external transceiver and a second CAN node. The existing test is TX-path only.
- **AD3 GND row 13**: `AD3_GND` in pinout.py maps to row 13 (top row, offset 12). This is the actual GND pin on the AD3 30-pin header, between the scope 2+ and V+ pins.

---

## Suggested Stage 3b plan order

1. Fix `test_dmm_hardware.py` ground topology (5 min — described above).
2. Run DMM hardware test, fix any pydwf backend issues.
3. Run SPI hardware test, fix any issues.
4. Run UART hardware test, fix any issues (likely the parity_error type).
5. Run CAN hardware test, fix any issues.
6. Write and run `test_logic_record_hardware.py`.
7. Review all hardware tests together — check for missing jumperless marker comments, inconsistent patterns, or missing edge cases.
8. Update CLAUDE.md session log and memory after completing.

---

## What is explicitly NOT in scope for Stage 3b

- **Passive sniff/decode mode** for SPI/UART/CAN (the design doc mentions it; it has not been started and requires significant DWF API work)
- **Multi-device support** (single device per server instance, by design)
- **CAN bidirectional test** (needs hardware transceiver)
- **SPI/UART hardware tests with real target devices** (loopback is sufficient for protocol validation)
- **Scope recording above 1MHz** (the AD3 has hardware limits on record mode sample rates; buffer mode supports higher rates)
