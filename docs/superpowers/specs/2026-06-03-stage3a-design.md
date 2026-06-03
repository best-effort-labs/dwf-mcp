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
logic.set_trigger(source, pin?, level?, condition?, position_s?, timeout_s?)
logic.capture(output_path?, format="npz"|"vcd")
logic.record_start(pins, sample_rate_hz, duration_s, output_path?, format="npz"|"vcd")
logic.record_status(record_id)
logic.record_stop(record_id)
```

`pins` is a list of DIO pin names (e.g. `["dio0", "dio1"]`). `format` defaults to `"npz"`. `source` for `set_trigger` is one of `"none"`, `"detector_digital_in"`, `"external1"` (T1), `"external2"` (T2). Trigger output routing is deferred to a later stage; use the external trigger pins as inputs only in 3a.

`record_start` is a standalone path — it does not require a prior `logic.configure` call. It configures `DigitalIn` for record mode internally, claims `pins`, and returns a `record_id` immediately. Buffer-mode (`logic.configure` → `logic.capture`) and streaming (`logic.record_start`) are independent tool paths that cannot be active simultaneously on the same instrument instance.

### Buffer-mode path (`logic.capture`)

Mirrors scope lifecycle exactly: configure arms `DigitalIn`, set_trigger configures trigger, capture polls `DigitalIn.status(readData=True)` until `DwfState.Done`, reads samples, writes artifact.

**Pin allocation:** `logic.configure(pins, ...)` calls `allocator.claim("logic", pins)`. Partial-failure rollback: if any backend call raises, release the claim and leave instance state as unconfigured (same pattern as Scope). `record_start` also claims its `pins` list via `allocator.claim("logic", pins)`, replacing any prior buffer-mode claim. Both paths share the same allocator slot (`"logic"`).

**Artifact formats:**

- `"npz"`: `ArtifactWriter.write_npz` with a uint8 array of shape `(n_samples, n_pins)`, pin names in config sidecar. Same writer as scope.
- `"vcd"`: calls `vcd_writer.write(path, samples, pin_names, sample_rate_hz)`. If `pyvcd` package is not installed, raises with message: `"VCD format requires the 'pyvcd' package: pip install dwf-mcp[vcd]"`.

### VCD optional extra

`pyproject.toml` additions — add `vcd` as a new optional extra and merge it into the existing `dev` extra:
```toml
[project.optional-dependencies]
vcd = ["pyvcd"]
dev = ["pytest", "ruff", "mypy", "pyvcd", ...]  # add "pyvcd" to existing dev list
```

The PyPI package is `pyvcd`; the import name is `vcd` (`from vcd import VCDWriter`).

In `logic.py`:
```python
try:
    import vcd as _vcd_pkg  # installed as pyvcd
    HAS_VCD = True
except ImportError:
    HAS_VCD = False
```

### VCD writer (`dwf_mcp/vcd_writer.py`)

Thin module (~50 lines) wrapping the `pyvcd` package (`import vcd`). Takes `path: Path`, `samples: np.ndarray` (uint8, shape `(n_samples, n_pins)`), `pin_names: list[str]`, `sample_rate_hz: float`. Computes timescale from sample rate, iterates transitions, writes VCD file. Accompanied by `test_vcd_writer.py` that round-trips a synthetic array through the writer and verifies output with the `vcd` reader.

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
    try:
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
    except asyncio.CancelledError:
        raise  # propagate normally — cancellation is not an error
    except Exception as exc:
        session.error = str(exc)
        session.done = True
```

`record_status` response shape: `{"record_id": str, "done": bool, "chunks_received": int, "lost_samples": int, "error": str | None}`. If `error` is non-null, the session failed; `record_stop` should still be called to clean up and write whatever data was accumulated.

#### `record_stop` sequence

1. Cancel the background task (`task.cancel()`, `await task` with `suppress(CancelledError)`)
2. Stop hardware acquisition: `backend.logic_record_stop()` (disarms `DigitalIn`)
3. Drain any remaining available samples from device with `logic_record_status()` + `logic_record_read()`
4. Write artifact (best-effort: if artifact writing fails, log the exception, include `"artifact_error"` in the response, do not raise — data loss at write time should not hide that recording completed)
5. Remove session from `self._sessions`
6. Release pin claim: `allocator.release("logic")`
7. Return `{"record_id": str, "artifact_path": str | None, "lost_samples": int, "error": str | None, "artifact_error": str | None}`

#### Session storage

`self._sessions: dict[str, _RecordingSession]` on the `Logic` instance.

#### Backend surface

Buffer-mode and record-mode use separate configure calls because they set different `DigitalIn` acquisition modes (`Single` vs `Record`) and have different parameter shapes.

```python
# Buffer mode
logic_configure(pin_mask: int, sample_rate_hz: float, buffer_size: int)
    # Sets acquisition mode=Single, sample rate, buffer size, enables channels
logic_set_trigger(source, pin_idx?, level?, condition?, position_s?, timeout_s?)
logic_arm()                              # DigitalIn.configure(reconfigure=False, start=True)
logic_status() -> str                    # "Done", "Armed", "Triggered", etc.
logic_read(count: int) -> np.ndarray     # shape (count, 16), uint8

# Record mode
logic_record_configure(pin_mask: int, sample_rate_hz: float)
    # Sets acquisition mode=Record, sample rate; no buffer_size (record mode uses streaming)
logic_record_arm()                       # starts record acquisition
logic_record_status() -> tuple[int, int, int]   # available, lost, remaining samples
logic_record_read(count: int) -> np.ndarray     # shape (count, 16), uint8 chunk
logic_record_stop()                      # aborts acquisition, disarms DigitalIn
```

Note: `pin_mask` is an integer bitmask (bit N = DIO pin N); pin name→mask translation happens in the instrument layer. `logic_read` and `logic_record_read` always return all 16 channels; the instrument layer slices to the configured pins before writing the artifact.

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

`dio.set_direction` is **purely local** — it only updates `self._directions: dict[str, str]` and does **not** touch hardware or claim the pin. This prevents `set_direction` from mutating shared `DigitalIO` state while the pin is owned by Pattern, Logic, or another instrument. The hardware direction register is written inside `dio.set` / `dio.read`, after the pin claim succeeds.

Call sequence for `dio.set(pin, state)`:
1. Check `self._directions[pin] == "out"` (raises `ValueError` if wrong)
2. `allocator.claim("dio", [pin])` (raises `PinAllocationError` if held elsewhere)
3. `backend.dio_set_direction(pin_idx, output=True)` + `backend.dio_set(pin_idx, state)`
4. `allocator.release("dio")`

`dio.read(pin)` mirrors this with `output=False` and `backend.dio_read`.

### Defaults and validation

If `set_direction` has not been called for a pin, default direction is `"in"` (safe). `dio.set` on an `"in"`-direction pin raises `ValueError` before attempting the claim.

### Backend surface

```python
dio_set_direction(pin_idx: int, output: bool)  # called inside set/read after claim
dio_set(pin_idx: int, state: bool)
dio_read(pin_idx: int) -> bool
```

---

## Testing Strategy

### Unit tests

| File | Key cases |
|---|---|
| `test_awg.py` | Configure/start/stop state machine; safety gate rejection; partial-failure rollback; `upload_custom` shape validation |
| `test_logic.py` | Buffer-mode capture cycle with pin claim/release; npz artifact written; VCD invoked when `format="vcd"`; `ImportError` on missing `pyvcd` (monkeypatch `HAS_VCD=False`); `record_start` claims pins; `_RecordingSession` lifecycle (start/status/stop); lost-sample counter propagated; backend exception in loop sets `session.error` and `done=True`; `record_stop` calls `logic_record_stop()` before artifact write |
| `test_pattern.py` | Configure/start/stop; `SafetyViolation` on wrong voltage; per-pin claim accumulation |
| `test_dio.py` | Default direction is `"in"`; `PinAllocationError` if pin claimed elsewhere; `set` on `"in"` pin raises before claim attempt; `set_direction` does not touch hardware; hardware direction applied inside `set`/`read` after claim |
| `test_vcd_writer.py` | Synthetic array round-trip through writer + pyvcd reader; timescale correct |

`FakeBackend` gets all new backend methods using the existing `record_call` + canned-response pattern. `logic_status()` returns `"Done"` after first call. `logic_record_status()` returns `(n, 0, 0)` on final call (remaining=0 terminates the loop).

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
