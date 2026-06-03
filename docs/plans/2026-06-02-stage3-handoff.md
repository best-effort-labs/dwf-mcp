# Handoff: stage 2 → stage 3

**Date:** 2026-06-02
**Audience:** A fresh Claude Code session picking up the dwf-mcp project after stage 2.

This file is meant to be self-contained. Read it first.

## TL;DR

Stage 2 (vertical slice: scope + supply + i2c) is merged to `main`. Architecture works end-to-end against a real AD3. Stage 3's job is to fill in the remaining instruments — **awg, logic, pattern, dio, dmm, can, spi, uart** — plus streaming/recording modes and the VCD writer. It's a substantially bigger surface than stage 2; plan for it to span multiple sessions.

## Project location

- Working directory: `/Users/tymm/Documents/claude-code/dwf-mcp`
- Branch: `main` (stage 2 merged; no in-flight branches)
- Worktree convention: `.worktrees/<branch>` (gitignored)
- venv: `.venv/` (gitignored, in repo root) — Python 3.11+

## Quick verification

Confirm the environment is healthy before starting:

```bash
cd /Users/tymm/Documents/claude-code/dwf-mcp
. .venv/bin/activate
pytest -m "not hardware"      # expect: 107 passed, 4 deselected
ruff check .                  # expect: All checks passed!
mypy src/dwf_mcp              # expect: Success: no issues found in 18 source files
```

If `.venv` is missing (it's gitignored), recreate it:

```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install --quiet -e ".[dev]"
```

## Read these first

In order, before doing anything else:

1. `docs/plans/2026-06-02-dwf-mcp-design.md` — the validated design for the whole server. Defines tool surface, safety layer, artifact format, extensibility model. **Skim, don't reread word-for-word** — stage 2 has been delivered against this; you mostly need to know which instruments and modes are still TODO.
2. `docs/plans/2026-06-02-stage2-design.md` — the validated architectural design from stage 2. Captures the `tools`-map dispatch, safety gate, exception mapping, artifact ownership, and pydwf method mapping for scope/supply/i2c. Many decisions here generalize to stage 3 instruments.
3. This file.

Optional but useful:
- `README.md` — short status block.
- `docs/plans/2026-06-02-stage2-implementation.md` — the executed plan, useful as a template for stage 3's plan style. Especially Task 5 (Scope) and Task 8 (Supply) as worked examples of how to spec an instrument.

## What's already built (stage 1 + stage 2)

| Layer | Module | Purpose |
|---|---|---|
| Safety | `policy.py` | `SafetyPolicy` frozen dataclass; voltage/current/amplitude caps + `SafetyViolation` |
| Safety chokepoint | `device.py::DwfDevice.gate_output` | Centralized "output goes hot" gate. Checks policy, writes `dwf-safety.log`, raises `SafetyViolation`. Strict on missing params. |
| Allocation | `allocator.py` | `PinAllocator` with `ResourceGroup` constraints, atomic replacement semantics for re-claims |
| AD3 metadata | `devices/ad3.py` | Pin lists + provisional resource groups. **Still provisional** — confirming pre-stage-3 is the right time. |
| Artifacts | `artifacts.py` | `ArtifactWriter.write_npz(instrument, arrays, config, summary)` writes `.npz` + JSON sidecar. Owned by `DwfMcpApp`. |
| Instrument ABC | `instrument.py`, `registry.py` | `Instrument` ABC with `tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]]` map and `__init__(device, artifacts)`. `InstrumentNotConfigured` exception. |
| Backend ABC | `backend.py` | `DwfBackend` ABC with scope/supply/i2c methods. Pattern: new instrument-level methods added as `raise NotImplementedError` defaults so backends fill them in lazily. |
| Backends | `backends/fake.py`, `backends/pydwf_backend.py` | Hardware-free fake (records calls + canned responses) and real pydwf passthroughs |
| Device session | `device.py` | `DwfDevice` — lazy open, idle timeout, unplug recovery, `workspace` property w/ setter |
| MCP app | `server.py::DwfMcpApp` | `register_instrument(cls)` walks tools map, lazy-instantiates, dispatches. Exception → result-shape mapping in `call_tool`. |
| Scope | `instruments/scope.py` | Buffer-mode acquisition. `configure → set_trigger → capture` with state machine and partial-failure rollback. |
| Supply | `instruments/supply.py` | `set → enable → disable → read`. Safety-gated enable routes through `device.gate_output`. Partial-failure rollback in `set`. |
| I2C | `instruments/i2c.py` | Active master via `pydwf.ProtocolI2C`. `configure → write/read/write_read/scan`. Partial-failure rollback in `configure`. |

**Exposed MCP tools:**
- Meta: `waveforms.open`, `waveforms.close`, `waveforms.status`, `waveforms.list_pins`
- Scope: `scope.configure`, `scope.set_trigger`, `scope.capture`
- Supply: `supply.set`, `supply.enable`, `supply.disable`, `supply.read`
- I2C: `i2c.configure`, `i2c.write`, `i2c.read`, `i2c.write_read`, `i2c.scan`

**Hardware-verified on real AD3:** Backend enumerate+open, scope (AWG 1 kHz sine → capture → freq estimate), supply (vpos round-trip), i2c (scan runs).

## Patterns that became conventions during stage 2

These were learned the hard way — write them into stage 3 instruments from the start, not after a code review catches them.

### 1. Partial-failure rollback in any method that claims pins or sets backend state

When a method does `allocator.claim` + backend calls, a backend exception leaves the allocator with claims that don't match the instrument's internal state. Pattern (applied in `Scope.configure`, `Supply.set`, `I2C.configure`):

```python
prior_claims = sorted(self._claimed_for_this_instrument())
prior_state = self._snapshot_relevant_state()
self.device.allocator.claim(self.name, new_pin_list)
self._clear_relevant_state()
try:
    self.device.backend.do_thing_1(...)
    self.device.backend.do_thing_2(...)
except Exception:
    if prior_claims:
        self.device.allocator.claim(self.name, prior_claims)
    else:
        self.device.allocator.release(self.name)
    self._restore_relevant_state(prior_state)
    raise
self._set_new_state(...)
```

For **fresh-state** instruments (where each `configure` replaces everything — like Scope and I2C): on exception, simply `release(self.name)` and leave instance state as unconfigured.

For **accumulating** instruments (where individual rails/channels can be added independently — like Supply): on exception, restore the prior claim set and the prior setpoint for the channel.

### 2. Schema constants at module scope, referenced from `tools` map

```python
SCOPE_CONFIGURE_SCHEMA: dict[str, Any] = {...}

class Scope(Instrument):
    name = "scope"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure": ("configure", SCOPE_CONFIGURE_SCHEMA),
        ...
    }
```

Don't bury schemas inside the class. Don't pull them into a sibling `schemas/` directory — co-locating with the class wins on grep-locality.

### 3. Use `getattr(exc, "details", {})` in exception mapping

`DwfMcpApp.call_tool` maps `SafetyViolation`, `PinAllocationError`, `DwfDeviceLost`, `InstrumentNotConfigured` to result-shape errors. The `details` field defaults to `{}` via `getattr`. **Stage 3's new exception types should follow the same pattern** — add to `_ERROR_TYPES` in `server.py:23-28`, optionally expose a `details` attribute for structured context.

### 4. The `_Set = set` trick when method names shadow builtins

`Supply.set` shadows the `set` builtin. Python resolves bare `set` inside method bodies to the builtin correctly, but `set[str]` in type annotations inside the class body fails. Workaround: `_Set = set` at module top, use `_Set[str]` in annotations. Applies to any instrument with methods named `set`, `dict`, `list`, etc.

### 5. Tests that monkeypatch the backend should match the real signature

The strict-keyword `gate_output("supply_enable", channel=..., voltage=...)` signature means `monkeypatch.setattr(backend, "scope_set_acquisition", boom)` needs `def boom(**kwargs)` not `def boom()`. Several Task-5 and Task-8 tests had to wrap the boom-function this way.

## Real pydwf API quirks discovered

These bit us in stage 2; record them so stage 3 doesn't relearn:

1. **`enum.serialNumber(i)`**, NOT `deviceSerialNumber(i)`. (Stage 1 caught this; reverified in stage 2.)
2. **`AnalogIO.channelName(i)` and `channelNodeName(i, j)` return tuples** — typically `(name, label)` and `(name, units)`. Use `[0]`.
3. **`AnalogIO.channelInfo(i)` returns a plain int** (node count), not a tuple.
4. **`ProtocolI2C.read(addr, length)` and `writeRead(addr, write, length)` return `Tuple[int, List[int]]`** — NAK count + data list, not bytes-like. Unpack with `_nak, data = ...` and wrap with `bytes(data)`.
5. **`ProtocolI2C.write(addr, data)` and `writeOne(addr, byte)` accept `List[int]` / `int`**, not `bytes`. Convert inputs with `list(data)`.
6. **I2C 7-bit address → 8-bit wire address**: pydwf's protocol calls expect `address << 1` (the 7-bit shifted left so the LSB is the R/W bit). The instrument layer uses 7-bit; the shift lives in `PydwfBackend`.
7. **Protocol accessor**: `self._device.protocol.i2c` (not `protocolI2C`). `digitalI2c` exists but is deprecated.
8. **`DwfState.Done` is the terminal state for scope capture polling.** Other states (Armed/Triggered/etc.) return as their `.name`.

## Method-discovery pattern (use whenever stage 3 needs a new DWF call)

```bash
. .venv/bin/activate && python -c "from pydwf import <Class>; print(sorted(m for m in dir(<Class>) if not m.startswith('_')))"
```

For checking signature/docstring:

```bash
. .venv/bin/activate && python -c "
import inspect
from pydwf import <Class>
sig = inspect.signature(<Class>.<method>)
print(f'{<method>}{sig}')
print((getattr(<Class>, '<method>').__doc__ or '')[:300])
"
```

## Real-silicon findings from stage 2

These will matter when wiring stage 3 instruments and their hardware smokes:

1. **AD3 V+ supply ramp is ~300ms** from 0V to setpoint (measured: 0.24V@100ms, 0.86V@200ms, 0.9995V@300ms). Hardware smokes need ≥500ms settle time after enable.
2. **Supply rail decay is load-dependent.** With no load, the output cap holds residual charge for many seconds after disable. Smoke tests should verify *state* (`enabled is False`) not voltage decay, unless a discharge resistor is wired in.
3. **macOS network-permission prompt on first run**: libdwf's `enumerateDevices()` scans for both USB and network-attached devices. The network probe triggers macOS's network access prompt. **Stage 3 follow-up candidate: add `DwfEnumFilter.USB` to `PydwfBackend.enumerate()`** to suppress the network scan (local-first principle from CLAUDE.md).
4. **Network discovery isn't phoning home** — it's local UDP/mDNS for `dwfsrv` instances. Benign, but unnecessary for USB-only setups.

## Stage 3 scope

From the design doc, **stage 3** is the remaining active instruments + recording/streaming modes:

### Active masters (in addition to the existing i2c)

#### awg (analog out) — safety-gated, mirrors supply's enable pattern
Tools:
- `awg.configure(channel, function, frequency_hz, amplitude_v, offset_v, phase_deg, symmetry, run_time_s?)`
- `awg.upload_custom(channel, samples_npy_path)` — custom waveform from .npy
- `awg.start(channel)` — calls `device.gate_output("awg_start", channel, amplitude=...)` first
- `awg.stop(channel)`

Backend surface to add on `DwfBackend`: ~6 methods mirroring `pydwf.AnalogOut` (channel + Carrier node selection, function/freq/amp/offset/symmetry set, configure, start, stop).

`SafetyPolicy.awg_max_amplitude` already exists and is plumbed through `gate_output`'s `awg_start` kind. The hardness is already in place — the instrument just needs to call it.

#### pattern (digital out)
Tools:
- `pattern.configure(pin, function, frequency_hz, duty, idle_state)`
- `pattern.start(pin)` — gated by `pattern_voltage` policy
- `pattern.stop(pin)`

Backend surface to add: ~5 methods on `pydwf.DigitalOut`. The `pattern_voltage` field in `SafetyPolicy` is currently a string ("3.3", "1.8") — pattern.start should refuse to start if the AD3 isn't configured for the policy's voltage. Note: AD3 has only one DIO voltage (set globally via DigitalIO); per-pin voltage isn't a thing.

#### spi (active master)
Tools:
- `spi.configure(sclk, mosi, miso, cs, mode, bit_order, clock_hz)`
- `spi.transfer(cs_assert, write_bytes, read_length)`

Backend surface: `pydwf.ProtocolSPI` — methods `clockSet`, `mosiSet`, `misoSet`, `clkPhaseSet`, `clkPolaritySet`, `clkOrderSet`, `read`, `write`, `writeRead`, etc. Check exact names + return shapes per the discovery pattern. Apply the I2C learning: return shapes likely include NAK-style status + data tuples.

#### uart
Tools:
- `uart.configure(tx_pin, rx_pin, baud, bits, parity, stop)`
- `uart.write(data)`
- `uart.read(timeout_s, max_bytes)`

Backend surface: `pydwf.ProtocolUART`. UART is byte-streaming — be careful with the rx polling loop (busy-spin risk same as scope's status poll).

#### can
Tools:
- `can.configure(tx_pin, rx_pin, bitrate)`
- `can.send(id, data, extended?)`
- `can.receive(timeout_s, max_frames)`

Backend surface: `pydwf.ProtocolCAN`. Sniff mode is stage 4 (passive decoders); active master only here.

### Passive instruments

#### logic (digital in / sniff) — most code in stage 3 likely lives here
Tools:
- `logic.configure(pins, sample_rate_hz, buffer_size)`
- `logic.set_trigger(pattern|edge|protocol_aware, ..., trigger_in_pin?, trigger_out_pin?)`
- `logic.capture(output_path?, format=npz|vcd)` — adds the VCD writer
- `logic.record(output_path?, duration_s, ...)` — streaming mode

Backend surface: `pydwf.DigitalIn`. Mirrors scope's lifecycle (configure → set_trigger → capture/record). The VCD writer is a stage-3 first.

#### dio (bidirectional GPIO)
Tools:
- `dio.set_direction(pin, in|out)`
- `dio.set(pin, state)` / `dio.read(pin)`

Backend surface: `pydwf.DigitalIO` (different from `DigitalIn`/`DigitalOut`). Allocator integration: each DIO is a single pin that the dio instrument claims as needed.

#### dmm (single-shot voltmeter)
Tools:
- `dmm.read(channel, mode=dc|ac|peak, samples?)`

Backend surface: `pydwf.AnalogIn` (re-uses the scope channels for single-shot measurement). Watch for resource conflicts with the scope — both claim scope1/scope2.

### Streaming / recording modes

`scope.record` and `logic.record` add streaming. Don't underestimate this — DWF's record mode has its own state machine (`statusRecord`, `statusSamplesLeft`, chunked reads), backpressure handling, and the call needs an async polling loop that doesn't block the MCP event loop.

### VCD writer

Lands with `logic.capture(format="vcd")`. Options: hand-roll (~200 lines) or use the `vcd` PyPI package. The design doc flagged this as "decide on first logic-capture implementation."

### AD3 pin map — verification becomes load-bearing here

The provisional pin map in `src/dwf_mcp/devices/ad3.py` is roughly right for stage 2's three instruments but starts to matter much more in stage 3:

- AWG channels share a clock domain — `awg_clock` resource group already exists but the exclusivity semantics need a real check.
- Logic capture vs. pattern output on the same DIOs — who wins?
- Trigger I/O routing (`trig1`/`trig2`) is currently unused.
- DIO 0–15 layout, reserved pins, and voltage-level constraints (3.3V vs 1.8V).

**Recommendation: spend ~30 minutes with the AD3 reference manual at the start of stage 3 verifying the pin map**, before wiring AWG (which is the first instrument to claim awg-clock-grouped pins). The AD3 reference is at https://digilent.com/reference/test-and-measurement/analog-discovery-3/reference-manual.

## Open questions to resolve during stage 3

1. **VCD writer choice** — hand-roll vs. `vcd` package. Decide before `logic.capture(format="vcd")`.
2. **Streaming concurrency model** — do `scope.record` / `logic.record` run inside the asyncio.Lock-protected `call_tool`, or fork to a background task that publishes events? The current single-lock model serializes the tool surface, which is fine for ≤2s captures but painful for multi-second records.
3. **dio pin conflicts** — should `dio.set(pin)` raise `PinAllocationError` if the pin is claimed by another instrument, or take ownership transiently? Probably the former.
4. **dmm vs scope resource conflict** — `dmm.read` and `scope.capture` both want scope1/scope2. Allocator catches it, but is the user-experience right? Maybe dmm should auto-release the scope claim, or maybe it should require explicit `scope.release()` first.
5. **AWG configure semantics** — `awg.configure` sets parameters but does NOT start; `awg.start` does the safety-gated start. Mirror Supply's pattern. Whether `awg.upload_custom` claims pins is a sub-question.
6. **CAN bitrate constraints** — pydwf's `ProtocolCAN` may have limited bitrates. Check before designing the schema.
7. **USB-only enumeration** — quick stage 3 prelude: add `DwfEnumFilter.USB` to `PydwfBackend.enumerate()` to skip the libdwf network scan (and the macOS network-permission prompt).

## Stage 3 risk profile

Stage 3 is **substantially larger than stage 2**:

| Metric | Stage 2 | Stage 3 (rough estimate) |
|---|---|---|
| Instruments | 3 (scope, supply, i2c) | 8 (awg, logic, pattern, dio, dmm, can, spi, uart) |
| New backend methods | ~16 | ~40+ |
| New tools exposed | 13 | ~25 |
| Backend recording/streaming | none | scope.record + logic.record |
| New artifact format | npz/json | + VCD |
| New shared infra | gate_output, register_instrument, exception mapping, ArtifactWriter | likely a streaming chunk-buffer helper |
| Hardware test wiring | W1 ↔ scope1+ | each instrument needs its own loopback |

**Realistic estimate: 25–35 plan tasks across 2–4 sessions, depending on how aggressive the user is about checking in between tasks.**

Token strategy that worked in stage 2 (Pro plan, weekly cap):
- Sonnet for mechanical backend extensions + spec-only-changes
- Opus for state-machine-heavy instruments (Supply, Scope) and judgment-heavy seams (`register_instrument`)
- Commit per task, no batching
- Check in with user between tasks rather than blasting through
- Bundle related hardening commits with their parent task review

## Suggested workflow for stage 3

1. **Brainstorm first** (`/superpowers:brainstorming`) — even more important than stage 2 because the streaming model and VCD writer have genuine design space.
2. **AD3 pin-map verification** — 30 minutes against the reference manual. Update `src/dwf_mcp/devices/ad3.py` with corrections. Single commit.
3. **USB-only enumeration tweak** — 5-minute follow-up to suppress the macOS network prompt.
4. **Worktree** (`/superpowers:using-git-worktrees`) — `.worktrees/stage3-<name>` off `main`.
5. **Plan** (`/superpowers:writing-plans`) — `docs/plans/2026-06-XX-stage3-implementation.md`. Given the size, **strongly consider splitting stage 3 into 3a (awg + logic + pattern + dio = the "primary new" group) and 3b (dmm + spi + uart + can + record/stream + VCD)**. Each as its own plan + execution session.
6. **Execute** (`/superpowers:subagent-driven-development`) — same TDD-per-task model. Apply the patterns under "Patterns that became conventions during stage 2" from the first commit, don't wait for reviewers to find them.

## Verification baseline (reproducible)

The numbers any new session should expect on a fresh `main` checkout:

- Test count: 107 passed, 4 deselected (hardware)
- Branch tip: `e91d90b`
- File count under `src/dwf_mcp/`: 18 (.py only; excluding `__init__.py` empties)
- No uncommitted changes in `git status`
- 4 hardware smoke tests scaffolded (pydwf_backend, scope_hardware, supply_hardware, i2c_hardware) — all `@pytest.mark.hardware`, skipped by default

To verify hardware tests against a connected AD3:

```bash
. .venv/bin/activate && pytest -m hardware -v
```

Requires: AD3 USB-attached, W1 jumpered to scope ch1+ (with GND to ch1- for clean differential reference). No external load on V+. I2C pull-ups optional (test accepts empty scan result).

## Memory tag for context continuity

If the user prefers it, you can save a memory tagged `dwf-mcp-stage2-complete` summarizing what landed, so subsequent sessions don't need to re-derive context from `git log`. Key facts to capture: tip SHA, instrument list, the conventions section above. Don't save anything already in this handoff doc — that's redundant.
