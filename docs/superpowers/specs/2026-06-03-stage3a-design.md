# Stage 3a Design: AWG, Logic, Pattern, DIO

**Date:** 2026-06-03
**Status:** Approved — ready for implementation planning
**Follows:** `docs/plans/2026-06-02-stage2-design.md` (architecture reference)
**Next stage:** Stage 3b covers dmm, spi, uart, can, scope.record, logic.record streaming, and VCD writer extensions.

---

## Scope

Stage 3a adds four instruments to the existing scope/supply/i2c set:

| Instrument | Backend API | New tools |
|---|---|---|
| AWG | `pydwf.AnalogOut` | `awg.configure`, `awg.upload_custom`, `awg.start`, `awg.stop` |
| Logic | `pydwf.DigitalIn` | `logic.configure`, `logic.set_trigger`, `logic.capture`, `logic.record_start`, `logic.record_status`, `logic.record_stop` |
| Pattern | `pydwf.DigitalOut` | `pattern.configure`, `pattern.start`, `pattern.stop` |
| DIO | `pydwf.DigitalIO` | `dio.set_direction`, `dio.set`, `dio.read` |

Total new tools: 16. Server surface after 3a: 29 tools (13 existing + 16 new).

No new shared infrastructure beyond:
- `dwf_mcp/vcd_writer.py` — thin wrapper around the `vcd` PyPI package
- `_RecordingSession` dataclass private to `logic.py`

---

## AWG Instrument (`instruments/awg.py`)

### Tool surface

```
awg.configure(channel, function, frequency_hz, amplitude_v, offset_v, phase_deg, symmetry, run_time_s?)
awg.upload_custom(channel, samples_npy_path)
awg.start(channel)
awg.stop(channel)
```

`channel` is 1 or 2 (W1/W2). `function` is one of: `"Sine"`, `"Square"`, `"Triangle"`, `"RampUp"`, `"RampDown"`, `"DC"`, `"Noise"`, `"Custom"`.

### State machine

Configure sets parameters and arms the hardware (pydwf parameter setters + `analogOut.configure(channel, False)`) but does **not** activate output. Start activates output via `analogOut.configure(channel, True)`. Stop calls `analogOut.configure(channel, False)` and releases nothing — pins remain claimed until the instrument is explicitly released.

### libdwf natural behavior

pydwf `AnalogOut` parameter setters (`frequencySet`, `amplitudeSet`, etc.) write to device registers immediately but do not activate output. `analogOut.configure(channel, start=False)` applies parameters without starting; `analogOut.configure(channel, start=True)` applies and starts. **Verify and document this in `PydwfBackend` method comments during implementation** — it is the foundation of the configure/start separation.

### Safety gate

`awg.start(channel)` calls:
```python
self.device.gate_output("awg_start", channel=channel, amplitude=self._amplitude[channel])
```
`_check_policy` already handles the `awg_start` kind (`policy.check_awg_amplitude`). If `gate_output` raises `SafetyViolation`, `analogOut.configure` is never called.

### Pin allocation (accumulating model, mirrors Supply)

`awg.configure(channel=1)` claims `["awg1"]`. Subsequent `awg.configure(channel=2)` claims `["awg1", "awg2"]`. The `awg_clock` exclusive resource group prevents any other instrument from holding either AWG pin while AWG is configured. Partial-failure rollback: on backend exception during configure, restore prior claim set and prior setpoints for the affected channel.

### `awg.upload_custom`

Loads `.npy` with `numpy.load`, validates shape is 1-D float64, calls `backend.awg_upload_custom(channel, samples)`. Claims pins identically to configure. Does not start output.

### Backend surface

```python
awg_configure(channel, function, freq_hz, amplitude_v, offset_v, phase_deg, symmetry, run_time_s)
awg_upload_custom(channel, samples: np.ndarray)
awg_start(channel)
awg_stop(channel)
```

---

## Logic Instrument (`instruments/logic.py`)

### Tool surface

```
logic.configure(pins, sample_rate_hz, buffer_size)
logic.set_trigger(source, pin?, level?, condition?, position_s?, timeout_s?, trig_in_pin?, trig_out_pin?)
logic.capture(output_path?, format="npz"|"vcd")
logic.record_start(pins, sample_rate_hz, duration_s, output_path?, format="npz"|"vcd")
logic.record_status(record_id)
logic.record_stop(record_id)
```

`pins` is a list of DIO pin names (e.g. `["dio0", "dio1"]`). `format` defaults to `"npz"`.

`record_start` is a standalone path — it does not require a prior `logic.configure` call. It configures `DigitalIn` for record mode internally and returns a `record_id` immediately. Buffer-mode (`logic.configure` → `logic.capture`) and streaming (`logic.record_start`) are independent tool paths that cannot be active simultaneously on the same instrument instance.

### Buffer-mode path (`logic.capture`)

Mirrors scope lifecycle exactly: configure arms `DigitalIn`, set_trigger configures trigger, capture polls `DigitalIn.status(readData=True)` until `DwfState.Done`, reads samples, writes artifact.

**Artifact formats:**

- `"npz"`: `ArtifactWriter.write_npz` with a uint8 array of shape `(n_samples, n_pins)`, pin names in config sidecar. Same writer as scope.
- `"vcd"`: calls `vcd_writer.write(path, samples, pin_names, sample_rate_hz)`. If `vcd` package is not installed, raises with message: `"VCD format requires the 'vcd' package: pip install dwf-mcp[vcd]"`.

### VCD optional extra

`pyproject.toml` additions — add `vcd` as a new optional extra and merge it into the existing `dev` extra:
```toml
[project.optional-dependencies]
vcd = ["vcd"]
dev = ["pytest", "ruff", "mypy", "vcd", ...]  # add "vcd" to existing dev list
```

In `logic.py`:
```python
try:
    import vcd as _vcd_pkg
    HAS_VCD = True
except ImportError:
    HAS_VCD = False
```

### VCD writer (`dwf_mcp/vcd_writer.py`)

Thin module (~50 lines) wrapping the `vcd` package. Takes `path: Path`, `samples: np.ndarray` (uint8, shape `(n_samples, n_pins)`), `pin_names: list[str]`, `sample_rate_hz: float`. Computes timescale from sample rate, iterates transitions, writes VCD file. Accompanied by `test_vcd_writer.py` that round-trips a synthetic array through the writer and verifies output with the `vcd` reader.

### Streaming path (`logic.record_start` / `_status` / `_stop`)

#### `_RecordingSession` dataclass (private to `logic.py`)

```python
@dataclasses.dataclass
class _RecordingSession:
    record_id: str          # UUID4
    task: asyncio.Task      # background polling loop
    queue: asyncio.Queue    # one np.ndarray chunk per DigitalIn.statusRecord read
    chunks: list[np.ndarray]  # accumulated for final artifact
    lost_samples: int
    done: bool
    error: str | None
```

The `queue` is the 3b streaming seam — MCP notification support attaches here without modifying the polling loop.

#### Background task loop

The loop is a coroutine method on `Logic` (not a free function), so it accesses `self.device.backend` directly:

```python
async def _record_loop(self, session: _RecordingSession) -> None:
    while not session.done:
        await asyncio.sleep(0.010)  # 10ms poll
        available, lost, remaining = self.device.backend.logic_record_status()
        session.lost_samples += lost
        if available > 0:
            chunk = self.device.backend.logic_record_read(available)
            session.chunks.append(chunk)
            await session.queue.put(chunk)
        if remaining == 0:
            session.done = True
```

#### Session storage

`self._sessions: dict[str, _RecordingSession]` on the `Logic` instance. `record_stop` cancels the task, drains remaining data, writes artifact, removes session from dict.

#### Backend surface

```python
logic_configure(pins: list[int], sample_rate_hz, buffer_size)
logic_set_trigger(source, pin_idx?, level?, condition?, position_s?, timeout_s?)
logic_arm()
logic_status() -> str
logic_read(count) -> np.ndarray          # buffer-mode read
logic_record_status() -> tuple[int, int, int]   # available, lost, remaining
logic_record_read(count) -> np.ndarray   # streaming chunk read
```

Note: `pins` passed to backend are integer DIO indices (0–15); pin name→index translation happens in the instrument layer.

---

## Pattern Instrument (`instruments/pattern.py`)

### Tool surface

```
pattern.configure(pin, function, frequency_hz, duty, idle_state)
pattern.start(pin)
pattern.stop(pin)
```

`function`: `"Pulse"`, `"Clock"`, `"Random"`, `"Custom"`. `idle_state`: `"low"`, `"high"`, `"hiz"`.

### Per-pin model (accumulating, mirrors Supply)

Each `pattern.configure(pin)` claims that DIO pin. Multiple pins can be configured independently. `pattern.stop(pin)` stops output on that pin but does not release the claim — consistent with AWG. `instrument.release()` (called by server on close) releases all.

### Safety gate

`pattern.start(pin)` calls:
```python
self.device.gate_output("pattern_start", pin=pin, voltage=self.device.policy.pattern_voltage)
```

Add `"pattern_start"` kind to `_check_policy` in `device.py` as a real check (not relying on the existing unknown-kind passthrough). On AD3, DIO voltage is fixed 3.3V. If `policy.pattern_voltage` is not `"3.3"` (string) or `3.3` (float), raise `SafetyViolation`: the hardware cannot comply.

### Backend surface

```python
pattern_configure(pin_idx, function, freq_hz, duty, idle_state)
pattern_start(pin_idx)
pattern_stop(pin_idx)
```

---

## DIO Instrument (`instruments/dio.py`)

### Tool surface

```
dio.set_direction(pin, direction)   # direction: "in" | "out"
dio.set(pin, state)                 # state: 0 | 1
dio.read(pin) -> int
```

### Allocation model (transient per-call)

`dio.set` and `dio.read` claim the pin, perform the operation, release the pin — all within a single call. If the pin is held by another instrument at call time, `PinAllocationError` is raised immediately before touching hardware.

`dio.set_direction` does not claim a pin; it only updates `self._directions: dict[str, str]`. Direction persists across calls.

### Defaults and validation

If `set_direction` has not been called for a pin, default direction is `"in"` (safe). `dio.set` on an `"in"`-direction pin raises `ValueError`. `dio.read` on an `"out"`-direction pin raises `ValueError`.

### Backend surface

```python
dio_set_direction(pin_idx, output: bool)
dio_set(pin_idx, state: bool)
dio_read(pin_idx) -> bool
```

---

## Testing Strategy

### Unit tests

| File | Key cases |
|---|---|
| `test_awg.py` | Configure/start/stop state machine; safety gate rejection; partial-failure rollback; `upload_custom` shape validation |
| `test_logic.py` | Buffer-mode capture cycle; npz artifact written; VCD invoked when `format="vcd"`; `ImportError` on missing `vcd` package (monkeypatch `HAS_VCD=False`); `_RecordingSession` lifecycle; lost-sample counter |
| `test_pattern.py` | Configure/start/stop; `SafetyViolation` on wrong voltage; per-pin claim accumulation |
| `test_dio.py` | Default direction is `"in"`; `PinAllocationError` if pin claimed elsewhere; `set` on `"in"` pin raises; transient claim/release |
| `test_vcd_writer.py` | Synthetic array round-trip through writer + vcd reader; timescale correct |

`FakeBackend` gets all new backend methods using the existing `record_call` + canned-response pattern. `logic_status()` returns `"Done"` after first call.

### Hardware smoke tests

| File | Wiring required |
|---|---|
| `test_awg_hardware.py` | W1 → scope ch1+ (existing smoke test wire) |
| `test_logic_hardware.py` | DIO0 → DIO1 loopback (pattern drives DIO0, logic captures DIO1) |
| `test_pattern_hardware.py` | Folds into logic hardware test |
| `test_dio_hardware.py` | DIO0 out, DIO1 in loopback |

**Expected baseline after 3a:** ~180–200 passed, 8 deselected (hardware).

---

## 3b Look-Ahead Notes

These are constraints preserved in 3a's design to avoid blocking 3b. No 3a implementation work required.

**`scope.record`:** `_RecordingSession` in `logic.py` is written as a clean, self-contained pattern. When 3b adds `scope.py` streaming, it copies the same structure independently. If both sessions are structurally identical after 3b, extract to `dwf_mcp/streaming.py` at that point — not before.

**`_RecordingSession.queue` as notification seam:** When MCP notification support lands (post-3b), a thin wrapper task reads from `queue` and calls `server.notification(...)`. No changes to the recording loop itself. `record_status` polling coexists with push.

**dmm / scope resource conflict:** `dmm` (3b) reuses `AnalogIn` with the same `scope1`/`scope2` pins. The `scope_pair` non-exclusive resource group handles this correctly — scope and dmm can each hold one channel. Document expected conflict behavior in the dmm instrument's docstring when written.

**SPI / UART / CAN:** All three claim specific DIO pins on configure (same model as I2C). No stage 3a instrument hard-codes DIO indices in a way that blocks these. The allocator handles conflicts automatically.

**`vcd` optional extra pattern:** All future optional extras follow the same pattern — try/import flag in the using module, extra in `pyproject.toml`, included in `dev`.
