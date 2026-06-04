# Stage 3b Design: DMM, SPI, UART, CAN, Streaming, VCD Extensions

**Date:** 2026-06-03
**Status:** Approved — ready for implementation planning
**Follows:** `docs/superpowers/specs/2026-06-03-stage3a-design.md`
**Next stage:** Stage 4 — protocol sniffing (passive bus capture), plot/image optional extra

---

## Scope

Stage 3b delivers three parallel workstreams:

| Workstream | Features | New / changed files |
|---|---|---|
| A — Instruments | DMM, SPI, UART, CAN | `instruments/dmm.py`, `instruments/spi.py`, `instruments/uart.py`, `instruments/can.py` |
| B — Streaming | `streaming.py` extraction, `scope.record`, MCP push notifications | `streaming.py` (new), edits to `logic.py`, `scope.py`, `server.py`, `backend.py` |
| C — VCD | Runtime config toggle, streaming VCD assembly | `vcd_writer.py` |

Workstream A is fully independent of B and C. Workstream C is also independent of B for `vcd_writer.py` changes, but both B and C modify `logic.py` (`record_stop` in particular) — coordinate to avoid conflicts rather than strict sequencing.

**Tool count after 3b:** ~43 tools (29 existing + 14 new: 1 DMM + 4 SPI + 3 UART + 3 CAN + 3 scope.record).

**Out of scope:** Server-side image/plot generation (LLM generates visualization code from npz artifacts). Protocol sniffing (passive bus capture) deferred to Stage 4.

---

## Workstream A: New Instruments

All four instruments follow the established pattern: JSON schemas per tool, `allocator.claim()` on configure, `allocator.release()` on `instrument.release()`. Backend stubs added to `backend.py` as `raise NotImplementedError` methods. `FakeBackend` gets canned responses using the existing `record_call` pattern.

### DMM (`instruments/dmm.py`)

Reuses `AnalogIn` hardware (same as scope). Single-call, stateless — no configure step. Each `dmm.measure()` call claims the pin transiently (claim → arm → wait → read → release within the call), mirroring the DIO transient model.

#### Tool surface

```
dmm.measure(channel, range_v, coupling="DC", n_averages=64)
→ {channel, mean_v, min_v, max_v, rms_v, range_v, coupling}
```

`channel`: 1 or 2. `range_v`: measurement range in volts (passed directly to `analogIn.channelRangeSet`). `coupling`: `"DC"` or `"AC"`. `n_averages`: number of samples to average (minimum 1, maximum 16384).

#### Implementation

1. `allocator.claim("dmm", ["scope1", "scope2"])` — claims the entire AnalogIn resource exclusively; raises `PinAllocationError` if scope holds either channel
2. `try:`
3. `backend.dmm_configure(channel, range_v, coupling, n_averages)` — sets AnalogIn to Single acquisition mode, configures the channel, sets sample count
4. `backend.dmm_arm()` — starts acquisition
5. Poll `backend.dmm_status()` until `"Done"` (fixed deadline: 2.0s)
6. `backend.dmm_read(channel, n_averages)` → `ndarray float64` of length `n_averages`
7. `finally: allocator.release("dmm")` — released even on exception or timeout
8. Return computed statistics

**AnalogIn exclusivity:** AnalogIn has a single shared acquisition engine — sample rate, buffer size, mode, and trigger are global across channels. DMM reconfiguring AnalogIn while a scope acquisition is armed would corrupt the scope state. DMM therefore claims both `scope1` and `scope2`, making it mutually exclusive with any scope operation. The `scope_pair` non-exclusive group does not help here — it allows two instruments to each hold a pin, but not to share the underlying hardware configuration. Document this constraint in `dmm.py` docstring.

**AnalogIn disarm on cleanup:** The `finally` block calls `backend.dmm_stop()` before `allocator.release("dmm")` to disarm AnalogIn even on timeout. Without this, a timed-out acquisition remains active in hardware and collides with a subsequent scope configure.

#### Backend surface

```python
dmm_configure(channel: int, range_v: float, coupling: str, n_averages: int) -> None
dmm_arm() -> None
dmm_status() -> str          # "Done", "Armed", etc.
dmm_read(channel: int, count: int) -> np.ndarray   # float64
dmm_stop() -> None           # disarms AnalogIn; called in finally before release
```

---

### SPI (`instruments/spi.py`)

Wraps `ProtocolSPI`. All configured pins claimed on configure; MOSI, MISO, CS are optional.

#### Tool surface

```
spi.configure(clk_pin, frequency_hz, mode, mosi_pin?, miso_pin?, cs_pin?,
              cs_polarity="active_low", bit_order="msb")
spi.transfer(data, assert_cs=True)   → {sent: [...], received: [...]}
spi.write(data, assert_cs=True)      → {bytes_written: N}
spi.read(length, assert_cs=True)     → {data: [...], data_hex: str}
```

`mode`: 0–3 (CPOL/CPHA). `data` in write/transfer/read: list of ints (0–255).

`assert_cs=False` means "do not assert or deassert CS within this transfer" — useful for chaining multiple back-to-back transfers while holding CS low. It does **not** route CS to a DIO instrument; SPI holds its configured `cs_pin` for its entire session. To control CS externally (e.g., via DIO), omit `cs_pin` from `spi.configure()` entirely — then no CS pin is claimed and DIO can drive it freely.

**Per-operation pin requirements** — raises `InstrumentNotConfigured` with a descriptive message if the required pin was not provided at configure time:

| Tool | Requires |
|---|---|
| `spi.transfer()` | `mosi_pin` and `miso_pin` |
| `spi.write()` | `mosi_pin` |
| `spi.read()` | `miso_pin` |
| `spi.transfer/write/read()` with `assert_cs=True` | `cs_pin` |

#### Pin allocation

`spi.configure(...)` calls `allocator.claim("spi", [p for p in [clk_pin, mosi_pin, miso_pin, cs_pin] if p is not None])`. Partial-failure rollback follows the I2C/Logic pattern: clear `_configured = False` before backend calls; on exception, `allocator.release("spi")` and re-raise. A failed reconfigure leaves the instrument unconfigured (same as I2C). This differs from AWG which restores prior state; for SPI the hardware state after a failed configure is indeterminate, so unconfigured is the only safe choice.

#### Backend surface

```python
spi_configure(clk_idx, freq_hz, mode, mosi_idx, miso_idx, cs_idx,
              cs_polarity, bit_order) -> None
    # mosi_idx / miso_idx / cs_idx are int | None
spi_transfer(data: bytes, assert_cs: bool) -> bytes    # full-duplex, returns MISO bytes
spi_write(data: bytes, assert_cs: bool) -> None
spi_read(length: int, assert_cs: bool) -> bytes
```

---

### UART (`instruments/uart.py`)

Wraps `ProtocolUART`. TX and RX claimed independently; either may be omitted.

#### Tool surface

```
uart.configure(baud_rate, tx_pin?, rx_pin?, data_bits=8, parity="none", stop_bits=1)
uart.write(data)                     → {bytes_written: N}
uart.read(length, timeout_s=1.0)     → {data: [...], data_hex: str, parity_error: bool}
```

`parity`: `"none"`, `"odd"`, `"even"`. `data` is list of ints. At least one of `tx_pin` / `rx_pin` must be provided; configure raises `ValueError` if both are `None`.

`uart.read` returns whatever bytes were received before the timeout — may be fewer than `length`. Callers must check `len(data)`. Empty `data` on timeout is not an error.

**Per-operation pin requirements** — raises `InstrumentNotConfigured` with a descriptive message if the required pin was not configured:

| Tool | Requires |
|---|---|
| `uart.write()` | `tx_pin` configured |
| `uart.read()` | `rx_pin` configured |

#### Backend surface

```python
uart_configure(baud_rate, tx_idx, rx_idx, data_bits, parity, stop_bits) -> None
    # tx_idx / rx_idx are int | None
uart_write(data: bytes) -> None
uart_read(length: int, timeout_s: float) -> tuple[bytes, bool]   # (data, parity_error)
```

---

### CAN (`instruments/can.py`)

Wraps `ProtocolCAN`. TX and RX both required.

#### Tool surface

```
can.configure(tx_pin, rx_pin, bit_rate)
can.send(id, data, extended=False)   → {sent: True}
can.receive(timeout_s=1.0)           → {id, data, data_hex, extended, error_count}
```

`id`: 11-bit (standard, `extended=False`) or 29-bit (extended). `data`: list of 0–8 ints. `error_count` is the hardware error counter from pydwf's `rx()` return value.

`can.receive` returns `{"id": None, "data": [], "data_hex": "", "extended": False, "error_count": 0}` on timeout — no exception raised. Callers check `id is None` to detect the timeout case.

#### Backend surface

```python
can_configure(tx_idx: int, rx_idx: int, bit_rate: int) -> None
can_send(id: int, data: bytes, extended: bool) -> None
can_receive(timeout_s: float) -> tuple[int | None, bytes, bool, int]  # (id, data, extended, error_count); id=None on timeout
```

---

## Workstream B: Streaming Infrastructure

### `streaming.py` — new shared module

Extracted from `logic.py`'s `_RecordingSession` and `_record_loop`. Made generic via poll/read callbacks.

```python
@dataclasses.dataclass
class RecordingSession:
    record_id: str
    task: asyncio.Task[None] | None               # hardware poll loop
    notification_task: asyncio.Task[None] | None  # MCP notification loop (separate)
    queue: asyncio.Queue[np.ndarray | None]       # bounded; None is stop sentinel
    chunks: list[np.ndarray]                      # accumulated for npz artifact assembly
    lost_samples: int
    done: bool
    error: str | None
    on_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None
    on_chunk_sync: Callable[[np.ndarray], None] | None = None
    # on_chunk_sync: called synchronously in the poll loop for each chunk before queue put.
    # Used by Logic for incremental VCD writing (see Workstream C). When set, chunks are NOT
    # appended to session.chunks (write-through avoids double memory for VCD format).
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    # meta carries instrument-specific fields needed for record_stop artifact assembly:
    # logic: {"pins", "sample_rate_hz", "output_path", "format"}
    # scope: {"channels", "range_v", "sample_rate_hz", "output_path"}
```

**Queue design:** `asyncio.Queue(maxsize=32)`. The poll loop uses `put_nowait`; if the queue is full the chunk is dropped from the notification path (recording is unaffected). A dropped notification is not an error.

**Two-task design:** The poll loop and notification delivery run as separate tasks so a slow or disconnected MCP client cannot block hardware polling.

**`process_chunk` helper:** Used by both the poll loop and the drain in `record_stop` to ensure consistent chunk handling:

```python
def process_chunk(session: RecordingSession, chunk: np.ndarray) -> None:
    """Route chunk to on_chunk_sync (VCD write-through) or chunks list (npz accumulation)."""
    if session.on_chunk_sync is not None:
        session.on_chunk_sync(chunk)   # may raise; caller sets session.error on exception
    else:
        session.chunks.append(chunk)
```

The notification queue put is separate and only in `record_loop` (not the drain path, since the notification task is cancelled before draining).

```python
async def record_loop(
    session: RecordingSession,
    poll_fn: Callable[[], tuple[int, int, int]],   # returns (available, lost, remaining)
    read_fn: Callable[[int], np.ndarray],
) -> None:
    try:
        while not session.done:
            await asyncio.sleep(0.010)
            available, lost, remaining = poll_fn()
            session.lost_samples += lost
            if available > 0:
                chunk = read_fn(available)
                try:
                    process_chunk(session, chunk)
                except Exception as exc:
                    session.error = str(exc)
                    session.done = True
                    return
                try:
                    session.queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass  # notification dropped; recording continues unaffected
            if remaining == 0:
                session.done = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        session.error = str(exc)
        session.done = True
    finally:
        # Best-effort sentinel; record_stop cancels notification_task explicitly.
        with contextlib.suppress(asyncio.QueueFull):
            session.queue.put_nowait(None)

async def notification_loop(
    session: RecordingSession,
    on_chunk: Callable[[str, np.ndarray], Awaitable[None]],
) -> None:
    while True:
        item = await session.queue.get()
        if item is None:
            break
        try:
            await on_chunk(session.record_id, item)
        except Exception:
            log.warning("notification send failed for record_id=%r", session.record_id)
            # Notification failure never sets session.error or stops recording.
```

`record_start` spawns both tasks:
```python
session.task = asyncio.create_task(record_loop(session, poll_fn, read_fn))
if on_chunk is not None:
    session.notification_task = asyncio.create_task(notification_loop(session, on_chunk))
```

`record_stop` cancels both tasks explicitly — it does not rely solely on the sentinel for notification task cleanup.

`logic.py` is updated to import `RecordingSession`, `process_chunk`, `record_loop`, and `notification_loop` from `streaming.py`. Its `_RecordingSession` dataclass and `_record_loop` method are deleted.

**Logic `record_start` rollback:** same pattern as scope (see below) — on backend or task creation failure, cancel tasks in reverse, `backend.logic_record_stop()`, close VCD writer, `allocator.release("logic")`.

**Logic `record_stop` exception-safety:** same `try/finally` pattern as scope — cleanup in `finally` regardless of backend/drain/artifact errors.

### `scope.record` — three new tools on `Scope`

Mirrors `logic.record_start/status/stop`, using `AnalogIn` record mode. Uses `RecordingSession` + `record_loop` from `streaming.py`.

#### Tool surface

```
scope.record_start(channels, range_v, sample_rate_hz, duration_s, output_path?,
                   offset_v=0.0, coupling="DC")
  → {record_id}
scope.record_status(record_id)
  → {record_id, done, chunks_received, lost_samples, error}
scope.record_stop(record_id)
  → {record_id, artifact_path, sidecar_path, lost_samples, error, artifact_error}
```

`channels`: list of 1 and/or 2. Artifact format: `npz` only — analog data is not representable in VCD.

#### Scope state machine

`Scope` gains `_mode: Literal[None, "buffer", "record"] = None`. The allocator alone does not prevent the same instrument instance from entering both modes simultaneously (an owner can re-claim its own slot). Explicit state is required.

Valid transitions:

| From `_mode` | `configure()` | `record_start()` |
|---|---|---|
| `None` | → `"buffer"` ✓ | → `"record"` ✓ |
| `"buffer"` | → `"buffer"` ✓ (replace config) | → `"record"` ✓ (implicit buffer release) |
| `"record"` | ✗ raises `RuntimeError` | ✗ raises `RuntimeError` |

Buffer mode has no active task — it is safe to overwrite or replace with a record session. `record_start()` when `_mode == "buffer"` implicitly releases the buffer config (`allocator.release("scope")`, `_config = None`, `_mode = None`) before proceeding with record setup. This matches how `logic.configure()` replaces a prior config.

Record mode has an active background task and possibly an open VCD writer. Neither `configure()` nor `record_start()` may enter while `_mode == "record"`. The caller must call `scope.record_stop(record_id)` first.

- `capture()`: requires `_mode == "buffer"`, raises `InstrumentNotConfigured` otherwise
- `record_status()` / `record_stop()`: require `_mode == "record"`, raise `ValueError` for unknown record_id
- `release()`: cancels any active record tasks, resets `_mode = None`, releases claim

#### Pin allocation

`allocator.claim("scope", [f"scope{c}" for c in channels])` — same allocator slot as buffer-mode scope. The `_mode` state machine above enforces mutual exclusivity between the two paths; the allocator claim prevents other instruments from using the same pins.

#### record_start rollback

If any step fails after resources are acquired, clean up in reverse:
1. `allocator.claim("scope", ...)` — if fails: nothing to clean up
2. If VCD format: `VcdStreamWriter.__init__()` — if fails: `allocator.release("scope")`
3. `backend.scope_record_configure()` — if fails: close writer if open, `allocator.release("scope")`
4. `backend.scope_record_arm()` — same cleanup as step 3
5. `asyncio.create_task(record_loop)` — if fails: `backend.scope_record_stop()`, close writer, `allocator.release("scope")`
6. `asyncio.create_task(notification_loop)` — if fails: cancel record_loop task, `backend.scope_record_stop()`, close writer, `allocator.release("scope")`

#### record_stop sequence (exception-safe)

```
try:
    1. Cancel and await record_loop task
    2. Cancel and await notification_task (explicit cancel, not sentinel-reliant)
    3. backend.scope_record_stop()       [log warning on failure, continue]
    4. Drain: scope_record_status() + scope_record_read() → process_chunk() per chunk
    5. Write artifact (best-effort; set artifact_error on exception)
except Exception:
    [all exceptions are swallowed here; errors reported in response fields]
finally:
    6. Close VCD writer if open (suppress exception)
    7. Remove session from self._sessions
    8. allocator.release("scope")
    9. self._mode = None
```

Cleanup in `finally` always runs regardless of exceptions in steps 1–5. Errors from backend stop/drain are logged but not surfaced in the response — only `error` (from acquisition) and `artifact_error` (from artifact write) are returned.

#### Backend additions

```python
scope_record_configure(channels: list[int], range_v: float, offset_v: float,
                        coupling: str, sample_rate_hz: float, duration_s: float) -> None
scope_record_arm() -> None
scope_record_status() -> tuple[int, int, int]   # available, lost, remaining
scope_record_read(count: int) -> np.ndarray     # shape (count, 2), float64 — always both channels
scope_record_stop() -> None
```

Instrument layer slices to configured channels before artifact assembly.

### MCP push notifications

`DwfMcpApp.call_tool` gains an optional parameter:

```python
async def call_tool(
    self,
    name: str,
    args: dict[str, Any],
    on_record_chunk: Callable[[str, np.ndarray], Awaitable[None]] | None = None,
) -> dict[str, Any]:
```

`_make_instrument_handler` checks `method_name == "record_start"`. If true, and `on_record_chunk` is not `None`, it injects `on_chunk=on_record_chunk` into kwargs before calling the method. All other tools are unaffected.

Instrument `record_start` methods gain `on_chunk: Callable | None = None` parameter (not part of the JSON schema — injected by the handler, not exposed to MCP callers). The parameter is passed directly into `RecordingSession.on_chunk`.

In `main()`:

```python
@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    session = server.request_context.session

    async def on_chunk(record_id: str, chunk: np.ndarray) -> None:
        await session.send_log_message(
            level="info",
            data=json.dumps({
                "event": "record_chunk",
                "record_id": record_id,
                "n_samples": chunk.shape[0],
                "dtype": str(chunk.dtype),
                "shape": list(chunk.shape),
                "data_b64": base64.b64encode(chunk.tobytes()).decode(),
            }),
        )

    return await app.call_tool(name, arguments, on_record_chunk=on_chunk)
```

Notifications use `send_log_message(level="info", ...)` with a JSON-encoded payload. Clients that want piecemeal chunks parse the payload; clients that don't ignore log messages. No new MCP schema registration required.

When `on_record_chunk` is `None` (unit tests, direct `call_tool` usage), recording works normally — notifications simply not sent.

---

## Workstream C: VCD Extensions

### Runtime configuration toggle

`build_app` gains `enable_vcd: bool | None = None`:
- `None` (default): VCD enabled iff `pyvcd` is installed (`vcd_writer.HAS_VCD`)
- `True`: VCD explicitly enabled; raises at startup if `pyvcd` not installed
- `False`: VCD explicitly disabled regardless of install state

`DwfMcpApp` stores `self.vcd_enabled: bool`. Tools that receive `format="vcd"` check this flag before `vcd_writer.HAS_VCD`. Error message when disabled: `"VCD output is disabled (set DWF_ENABLE_VCD=1 or install dwf-mcp[vcd])"`.

`main()` reads `DWF_ENABLE_VCD` env var (`"1"` / `"0"` / unset) and passes it to `build_app`.

`vcd_writer.HAS_VCD` remains the install-time sentinel; the runtime flag is an additional gate.

### Streaming VCD assembly (`VcdStreamWriter`)

`vcd_writer.py` gains a context-manager writer that appends transitions incrementally:

```python
class VcdStreamWriter:
    def __init__(self, path: Path, pin_names: list[str], sample_rate_hz: float) -> None: ...
    def write_chunk(self, chunk: np.ndarray) -> None: ...  # uint8, shape (N, n_pins)
    def close(self) -> None: ...
    def __enter__(self) -> VcdStreamWriter: ...
    def __exit__(self, *_: object) -> None: ...
```

`write_chunk` maintains a running sample counter and last-seen state per pin, appending only transitions to the open VCD file.

**Bounded-memory integration:** For VCD format, the writer is opened at `record_start` time and stored as `session.meta["vcd_writer"]`. `Logic.record_start` sets `session.on_chunk_sync = session.meta["vcd_writer"].write_chunk`. The `record_loop` in `streaming.py` calls `on_chunk_sync(chunk)` synchronously for each chunk and does NOT append to `session.chunks`. This gives truly bounded memory during acquisition — only the current chunk is in flight. At `record_stop`, `session.meta["vcd_writer"].close()` is called; no concatenation step.

For npz format, `on_chunk_sync` is `None` and `session.chunks` accumulates as before.

`logic.record_stop` artifact assembly:
- VCD format: call `session.meta["vcd_writer"].close()`, return path
- npz format: concatenate `session.chunks`, write npz as before

The existing `vcd_writer.write()` one-shot function is unchanged — still used by `logic.capture` (buffer-mode).

**Sync I/O tradeoff:** `on_chunk_sync` runs in the hardware poll loop, so disk write latency directly delays the next `analogIn`/`digitalIn` status poll. For local SSDs, VCD transition writes are typically sub-millisecond and this is acceptable. VCD recording is not recommended over network filesystems or slow storage — use npz format in those environments. A future optimization could offload VCD writes to a dedicated bounded task, but that re-introduces double-buffering and is deferred.

---

## Testing Strategy

### Unit tests

| File | Key cases |
|---|---|
| `test_dmm.py` | Transient claim/release per call; `PinAllocationError` when scope holds the channel; statistics computed correctly |
| `test_spi.py` | Configure pin claim; full-duplex transfer; write-only; read-only; `InstrumentNotConfigured` before configure; partial-failure rollback |
| `test_uart.py` | Configure; write; read with `parity_error=True`; `ValueError` if both tx and rx are `None` |
| `test_can.py` | Configure; send standard and extended frame; receive; error_count propagated |
| `test_streaming.py` | `record_loop` with synthetic poll/read fns; `on_chunk_sync` called per chunk before queue put; chunks not accumulated when `on_chunk_sync` set; `put_nowait` drops silently when queue full; `notification_loop` consumes queue; notification exception does not set `session.error`; cancellation propagates cleanly; backend exception sets `error` and `done=True`; `remaining==0` terminates loop |
| `test_scope.py` (additions) | `record_start/status/stop` lifecycle; `_mode` prevents simultaneous buffer+record; `RuntimeError` on mode conflict; pin claim/release; npz artifact written; `artifact_error` set on write failure |
| `test_logic.py` (updates) | All existing tests pass after `_RecordingSession` extraction — no behavior change; VCD path sets `on_chunk_sync` and skips chunks accumulation |
| `test_server_async.py` (additions) | `on_record_chunk` injected into `logic.record_start` and `scope.record_start` calls; not injected for other tools; `None` skips notification task |
| `test_vcd_writer.py` (additions) | `VcdStreamWriter` multi-chunk round-trip matches one-shot `write()` output; timescale and sample counter preserved across chunks |
| `test_dmm.py` (update) | Claims both scope1+scope2; `PinAllocationError` if scope holds either channel; claim released in finally even on timeout |

`FakeBackend` additions:
- All new backend methods use `record_call` + canned responses
- `dmm_status()` returns `"Done"` after first call
- `scope_record_status()` returns `(n, 0, 0)` on final call (remaining=0 terminates loop)

### Hardware smoke tests

| File | Wiring required |
|---|---|
| `test_dmm_hardware.py` | Scope ch1 probes a known voltage (e.g. W1 output at known DC level) |
| `test_spi_hardware.py` | MOSI → MISO loopback; CLK and CS to any free DIO |
| `test_uart_hardware.py` | TX → RX loopback |
| `test_can_hardware.py` | TX → RX loopback (requires 120Ω termination or short wire at low bit rate) |

**Expected baseline after 3b:** ~260–280 passed, 12 deselected (hardware).

---

## 4b Look-Ahead Notes

**Protocol sniffing (Stage 4):** Passive capture of SPI/UART/CAN/I2C traffic without driving the bus. Resource contention model differs from active-master: sniff mode is read-only and can coexist with other instruments observing the same pins. `DigitalIn` (Logic) record mode is the capture path; protocol decoders run in software on the npz artifact. Stage 3b active-master instruments do not hard-code assumptions that block sniff-mode later.

**Plot/image tools (Stage 4 or later):** Optional `[plots]` extra with matplotlib. Adds `*.plot()` tools returning MCP image content type for direct visual inspection by the LLM client. Deferred from 3b — LLM generates visualization code from npz + sidecar JSON instead.

**MCP notification schema:** `send_log_message` is used in 3b for chunk delivery as a pragmatic first step. If a more structured notification channel is warranted (e.g. a dedicated `notifications/dwf/record_chunk` method), migrate in a future stage without changing the instrument layer — only `main()`'s `on_chunk` closure needs updating.
