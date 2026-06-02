# Stage 2 — Implementation Design

**Date:** 2026-06-02
**Status:** Validated through brainstorming; ready for plan-writing.
**Inputs:** [`2026-06-02-dwf-mcp-design.md`](2026-06-02-dwf-mcp-design.md) (overall design), [`2026-06-02-stage2-handoff.md`](2026-06-02-stage2-handoff.md) (open questions).

## Purpose

Stage 2 wires the first three real instruments — **scope**, **supply**, **i2c** — onto the stage 1 foundation. The goal is a vertical slice that exercises the architecture end-to-end against a real AD3, so stage 3+ can add instruments by mechanical extension.

## Instrument shape

### Tools map

Each instrument is a subclass of `Instrument` that declares a class-level `tools` map from MCP-tool-suffix to `(method_name, input_schema_dict)`. `app.register_instrument(cls)` walks the map and registers `{instrument.name}.{suffix}` handlers that route to bound methods on a per-session instance.

```python
class Instrument(ABC):
    name: ClassVar[str]
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]]

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None: ...

    @abstractmethod
    def release(self) -> None: ...
```

Dropped from the design-doc lifecycle, on purpose:

- `configure()` from the ABC — supply has no configure step (`supply.set` does it).
- `required_pins` (was on the stage 1 ABC) — pins are dynamic per `configure` args, not class-level.
- `arm()` / `read()` — don't generalize across scope/supply/i2c. Each instrument's `tools` map declares whatever methods its tools need.

### Lifecycle

Instruments are created **lazily** on first tool call: `app.call_tool("scope.configure", ...)` checks `app.instruments["scope"]`, instantiates if missing, then dispatches. Instance lives for the session.

A second `configure` call **releases the old pin claim and re-allocates the new one** atomically (`allocator.release(instrument="scope")` → `allocator.claim(...)`), so re-configuring a scope range mid-bench works without ritual.

`waveforms.close` calls `release()` on every live instrument.

### Schemas

JSON Schema dicts are co-located in the same module as the instrument class as module-level constants (`SCOPE_CONFIGURE_SCHEMA = {...}`). Referenced from the `tools` map. `DwfMcpApp._list_tools` feeds the schemas through to the MCP SDK so Claude sees proper argument types.

## Safety gate

`DwfDevice.gate_output(kind: str, **params)` is the single chokepoint for any "output goes hot" path. It:

1. Calls `self.policy.check(...)` — raises `SafetyViolation` on rejection.
2. Appends a structured line to `<workspace>/dwf-safety.log`.
3. Returns (allowing the caller to touch the backend).

Stage 2's only caller is `Supply.enable`. Stage 3's `awg.start`, `pattern.start`, etc. will use the same helper — that's why it lives on `DwfDevice`, not on `Supply`.

Supply tool surface keeps the design-doc names: `set / enable / disable / read`. `enable` is the arm; renaming to `arm/disarm` would violate least-surprise for bench-psu users.

## Exception → MCP mapping

Centralized in `DwfMcpApp.call_tool`. One try/except around the dispatch translates known domain exceptions into a tool *result* of shape:

```json
{"error": {"type": "SafetyViolation", "message": "...", "details": {...}}}
```

Translated types:
- `SafetyViolation` (from `gate_output`)
- `PinAllocationError` (from allocator)
- `DwfDeviceLost` (from device / backend)
- `InstrumentNotConfigured` (new — raised when e.g. `scope.capture` runs without prior `scope.configure`)

Unknown exceptions bubble — the MCP SDK turns them into transport errors. Domain errors come back as results so Claude can reason about them ("ah, 5V exceeds the 3.3V cap — back off").

## ArtifactWriter ownership

One `ArtifactWriter` owned by `DwfMcpApp`, constructed at `__init__`, points at the device workspace. Passed into each instrument's `__init__`. If `waveforms.open(workspace_dir=...)` changes the workspace, the app rebuilds the writer. Instruments never own one.

## Concurrency

Single `asyncio.Lock` on `DwfMcpApp`, acquired in `call_tool` around the dispatch. Blocks for v1. A multi-second scope capture serializes the tool surface during the capture window — accepted cost to avoid needing arm-then-poll semantics. Revisit in stage 3 if pain shows up.

## pydwf surface mapping

Verified against `pydwf` 1.1.x in the project venv.

### scope → `pydwf.AnalogIn`

| Tool | Methods |
|---|---|
| `scope.configure` | `channelEnableSet`, `channelRangeSet`, `channelOffsetSet`, `channelCouplingSet`, `frequencySet`, `bufferSizeSet`, `acquisitionModeSet(Single)` |
| `scope.set_trigger` | `triggerSourceSet`, `triggerTypeSet`, `triggerChannelSet`, `triggerLevelSet`, `triggerConditionSet`, `triggerPositionSet`, `triggerAutoTimeoutSet` |
| `scope.capture` | `configure(reconfigure=False, start=True)`, poll `status(True)` until `Done`, `statusData(channel, buffer_size)` per enabled channel |

Buffer mode only for v1. Streaming (`scope.record`) deferred to stage 3.

### supply → `pydwf.AnalogIO`

AD3 supplies live on a "Supplies" channel with per-rail nodes. Discover at runtime: on `Supply.__init__`, iterate `channelCount` × `channelNodeCount`, match `channelName` / `channelNodeName` against the rail name (`vpos` / `vneg`) and node names (`enable` / `voltage` / `current`), cache `(channel_idx, node_idx)` triplets per rail. Robust to firmware revisions and trivially works for AD2 later.

| Tool | Methods |
|---|---|
| `supply.set` | (no backend call yet — store voltage/current_limit in instance) |
| `supply.enable` | `device.gate_output("supply_enable", ...)` → `channelNodeSet(voltage)`, `channelNodeSet(enable=1)`, `enableSet(True)` (master) |
| `supply.disable` | `channelNodeSet(enable=0)`. Master `enableSet(False)` only when no rails remain enabled. |
| `supply.read` | `channelNodeStatus(voltage)`, `channelNodeStatus(current)` for measured; instance state for requested |

### i2c → `pydwf.ProtocolI2C`

| Tool | Methods |
|---|---|
| `i2c.configure` | `reset`, `sclSet(dio_idx)`, `sdaSet(dio_idx)`, `rateSet(hz)`, `stretchSet(True)`, `timeoutSet(timeout_s)` |
| `i2c.write` | `write(address, bytes)` → returns nak count |
| `i2c.read` | `read(address, length)` → returns bytes |
| `i2c.write_read` | `writeRead(address, write_bytes, read_length)` |
| `i2c.scan` | loop addresses 0x08–0x77, call `writeOne(address, 0)`, collect ACKs |

Pullup configuration: stash on the instance; AD3's I2C protocol class doesn't expose pullup control directly — pullups are physical on the AD3, so `pullups` is informational metadata for the sidecar.

## AD3 pin map

Accept the provisional map in `src/dwf_mcp/devices/ad3.py` for stage 2. The pins stage 2's instruments touch are correct at the granularity the allocator cares about:

- `scope1`, `scope2` — claimed by scope
- `vpos`, `vneg` — claimed by supply (mapped to AnalogIO nodes at runtime)
- `dio0`–`dio15` — two-of-N claimed by i2c per configure

Add a `# TODO: verify against AD3 reference manual before stage 3` comment. Stage 3 (AWG/logic/pattern) is when co-sampling semantics, AWG clock domain, and reserved DIO assignments become load-bearing — verify then, not now.

## Testing

### Unit (no hardware)

- Each instrument tested via the `FakeBackend`. Extend `FakeBackend` with the minimum surface each instrument needs (recorded calls + canned responses). Aim: ≥1 test per tool covering golden path and one error case (e.g. `scope.capture` without prior `configure` raises `InstrumentNotConfigured`).
- `DwfMcpApp.call_tool` exception mapping: one test per translated exception type asserting the result shape.
- `Supply` node discovery: test against a `FakeBackend` AnalogIO surface with both correct and reshuffled node ordering.

### Hardware smoke (`pytest -m hardware`)

Requires a plugged-in AD3 with a small loopback harness (W1 ↔ scope 1+, AWG out wired to itself, etc.). Stays deselected in CI; runs locally pre-merge.

- **scope:** start AWG at 1 kHz sine, capture, assert `freq_estimate` ≈ 1 kHz ± 1%.
- **supply:** enable `vpos` at 1.0 V, read measured voltage, assert ≈ 1.0 V ± 50 mV. Disable. Assert measured ≈ 0 V.
- **i2c:** configure SDA/SCL on two DIOs (with bench pull-ups), run `scan()`, assert returns without error. Even an empty list proves the wire toggled.

### MCP integration

Existing stage 1 integration test pattern (spawn `DwfMcpApp` with `FakeBackend`, exercise tool sequences). Add `open → scope.configure → scope.set_trigger → scope.capture → close` and `open → i2c.configure → i2c.scan → close`.

## Out of scope (reaffirmed from handoff)

- `awg`, `logic`, `pattern`, `dio`, `dmm`, `can`, `spi`, `uart` — stage 3.
- Passive decoders (`decoder.i2c`, etc.) — stage 4.
- `scope.record` streaming mode — stage 3.
- VCD writer — stage 3 (lands with `logic.capture`).
- Per-device firmware version probing — defer until something needs it.

## Open implementation questions for the plan

These don't change the design but are worth resolving when writing the plan:

1. JSON Schema dialect — MCP SDK accepts standard JSON Schema; pick a minimal subset (type, required, properties, enum, minimum/maximum) and stick to it for stage 2.
2. Sidecar JSON shape per instrument — capture per-instrument config snapshot + active safety policy + pin claims. Define a `SidecarBuilder` helper on `ArtifactWriter` so each instrument doesn't reinvent the format.
3. Test scaffolding — whether to extend `FakeBackend` in one PR-able commit per instrument or split fake surface from instrument tests. Lean toward: each instrument task lands both at once (fake surface + instrument + tests + smoke marker).
