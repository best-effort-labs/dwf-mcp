# Handoff: foundation → stage 2

**Date:** 2026-06-02
**Audience:** A fresh Claude Code session picking up the dwf-mcp project.

This file is meant to be self-contained. Read it first.

## TL;DR

Stage 1 (foundation) is merged to `main`. The next chunk of work is stage 2: wire up the first three real instruments — **scope**, **supply**, and **i2c** — as a vertical slice that proves the architecture end-to-end against a real AD3.

## Project location

- Working directory: `/Users/tymm/Documents/claude-code/dwf-mcp`
- Branch: `main` (foundation already merged; no in-flight branches)
- Worktree convention: `.worktrees/<branch>` (gitignored)
- venv: `.venv/` (gitignored, in repo root) — Python 3.11+

## Quick verification

Confirm the environment is healthy before starting:

```bash
cd /Users/tymm/Documents/claude-code/dwf-mcp
. .venv/bin/activate
pytest -m "not hardware" -v   # expect: 37 passed, 1 deselected
ruff check .                  # expect: All checks passed!
mypy src/dwf_mcp              # expect: Success: no issues found in 14 source files
```

If any of those fail, that's the first thing to fix — don't pile new work on a broken foundation.

## Read these first

In order, before doing anything else:

1. `docs/plans/2026-06-02-dwf-mcp-design.md` — the validated design for the whole server. Defines tool surface, safety layer, artifact format, extensibility model.
2. `docs/plans/2026-06-02-foundation-implementation.md` — the stage 1 plan (already executed). Useful as a template for what stage 2's plan should look like, and for understanding conventions.
3. This file.

Optional but useful:
- `README.md` — short status block.
- Skim `src/dwf_mcp/` modules — small, ~14 files, easy to navigate.

## What's already built (stage 1)

Foundation is in place — instruments can plug in cleanly on top of it.

| Layer | Module | Purpose |
|---|---|---|
| Safety | `src/dwf_mcp/policy.py` | `SafetyPolicy` frozen dataclass with voltage/current/amplitude caps + `SafetyViolation` exception |
| Allocation | `src/dwf_mcp/allocator.py` | `PinAllocator` with `ResourceGroup` constraints |
| AD3 metadata | `src/dwf_mcp/devices/ad3.py` | Pin lists + provisional resource groups |
| Artifacts | `src/dwf_mcp/artifacts.py` | `ArtifactWriter` writes `.npz` + JSON sidecar |
| Instrument ABC | `src/dwf_mcp/instrument.py`, `registry.py` | `Instrument` ABC + `InstrumentRegistry` |
| Backend ABC | `src/dwf_mcp/backend.py` | `DwfBackend` ABC, `DeviceInfo`, `DwfBackendError`, `DwfDeviceLost` |
| Backends | `src/dwf_mcp/backends/fake.py`, `pydwf_backend.py` | Hardware-free fake + real pydwf |
| Device session | `src/dwf_mcp/device.py` | `DwfDevice` — lazy open, idle timeout, unplug recovery, status |
| MCP server | `src/dwf_mcp/server.py` | `DwfMcpApp` + stdio entry, four meta tools |

**Meta tools currently exposed:** `waveforms.open`, `waveforms.close`, `waveforms.status`, `waveforms.list_pins`. No instruments wired yet.

## Carry-forward design notes (from stage 1 subagents)

These were surfaced during stage 1 implementation. They affect how stage 2 should be built.

### From Task 7 (`DwfDevice`)

1. **Tools must call `device.require_open()` before doing work.** It both gates on liveness and resets the idle timer. The MCP server's existing `_tool_list_pins` already does this — follow the pattern.
2. **`is_open` has read-side effects** (clears `_info` and allocator when the backend has died). Treat it as the canonical health probe; don't refactor away the side effect.
3. **`status()` is not pure** — it calls `is_open`, which can clear state. That's intentional and consistent.
4. **`device.open(serial="A")` then `device.open(serial="B")` silently returns the A session.** Backend layer is permissive. The MCP server doesn't guard this yet. **If stage 2 surfaces a way for users to bump into this, consider raising at the tool layer when the requested serial differs from the held one.**
5. **`DwfDevice` is not thread-safe.** Fine for a single asyncio event loop; don't spawn threads that touch the device.

### From Task 8 (MCP server)

1. **Tool dispatch is via the `DwfMcpApp._tools` dict.** When adding instruments, the cleanest extension is a public `register_tool(name, handler)` method. Right now stage-1 code pokes `_tools` directly with a `# noqa: SLF001` — fine for the four meta tools but not great as a long-term pattern.
2. **`_list_tools()` returns empty descriptions.** When instruments declare schemas (input args, return shape), plumb those through to the MCP SDK's tool list. The SDK supports JSON Schema for tool args — use it.
3. **No domain-exception → MCP-error mapping yet.** When tools start raising `SafetyViolation`, `PinAllocationError`, `DwfDeviceLost`, etc., the server should translate these to MCP error responses rather than letting them bubble as Python exceptions. Add this when wiring the first instrument that can raise them.

### From Task 9 (`PydwfBackend`)

The plan's pydwf API names were partially wrong. Verified surface:

- `pydwf.DwfLibrary()` has `.deviceEnum` and `.deviceControl` (camelCase, as expected).
- `enum.enumerateDevices()` ✅
- `enum.deviceName(i)` ✅
- `enum.serialNumber(i)` — **NOT** `deviceSerialNumber`. The plan was wrong.
- `enum.deviceVersion(i)` **does not exist**. Currently falling back to `dwf.getVersion()` (libdwf runtime version) for the `firmware` field. Real per-device firmware probably requires `paramGet` or similar — defer until something actually needs it.
- `deviceControl.open(i)` ✅, returns a `DwfDevice` object with `.close()`.

**Method discovery pattern that worked:** activate the venv, then `python -c "from pydwf import DwfLibrary; print(dir(DwfLibrary().deviceEnum))"`. Use this whenever a stage-2 instrument needs a new DWF call.

### From Task 1

`pydwf` installs cleanly via pip on macOS without needing Digilent's WaveForms runtime first. The runtime is only needed at the moment a DWF function is actually called against hardware (i.e. `enumerateDevices()` on a machine with no runtime will fail at the libdwf load step, but `pydwf` import and `DwfLibrary()` instantiation are fine).

## Stage 2 scope

From the design doc, the three instruments for the vertical slice:

### scope (analog in) — buffer mode only for v1
Tools:
- `scope.configure(channels, range_v, offset_v, coupling, sample_rate_hz, buffer_size)`
- `scope.set_trigger(source, channel, level_v, edge|condition, position_s, timeout_s, trigger_in_pin?, trigger_out_pin?)`
- `scope.capture(output_path?)` — returns `{path, sidecar_path, summary: {min, max, mean, rms, freq_estimate, sample_rate, trigger_time}}`

Defer `scope.record` (streaming) to stage 3 — buffer mode is enough to prove the slice.

### supply — safety-gated programmable power
Tools:
- `supply.set(channel, voltage, current_limit?)` — checks `SafetyPolicy`, writes voltage, **does not enable**
- `supply.enable(channel)`, `supply.disable(channel)` — explicit
- `supply.read(channel)` — requested vs. measured
- Optional: `current_trip` behavior

This is where the `SafetyPolicy` integration gets exercised for the first time.

### i2c — active master
Tools:
- `i2c.configure(sda_pin, scl_pin, clock_hz, pullups)`
- `i2c.write(address, data)`, `i2c.read(address, length)`, `i2c.write_read(address, write, read_length)`, `i2c.scan()`

This is where the `PinAllocator` integration gets exercised, and where the new `register_tool` extension pattern should land.

### Out of scope for stage 2

- `awg`, `logic`, `pattern`, `dio`, `dmm`, `can`, `spi`, `uart` — stage 3
- Passive decoders (`decoder.i2c`, etc.) — stage 4
- Recording / streaming modes
- VCD writer

## Open questions to resolve during stage 2

1. **AD3 pin map.** The provisional map in `src/dwf_mcp/devices/ad3.py` was guessed from AD2 conventions. Verify against the AD3 reference manual before wiring real pins. Particularly:
   - Confirm DIO0–15 layout and any reserved pins
   - Confirm scope channel co-sampling semantics (does claiming one really lock the pair?)
   - Confirm AWG shared-clock domain
   - Confirm supply pin names (currently `vpos`, `vneg`)
2. **`MCP SDK tool schemas.** Stage 2 should plumb JSON Schema declarations through `_list_tools()` so Claude can see proper argument types. The `mcp` SDK supports this; investigate the exact decoration before designing the instrument modules.
3. **`ArtifactWriter` wiring into `DwfDevice`.** Currently `workspace` is stored on `DwfDevice` but no `ArtifactWriter` is constructed. Stage 2 needs to decide whether the writer lives on the device, on the app, or is created per-instrument. Recommended: one writer on the app, shared across instruments.
4. **Exception-to-MCP-error mapping.** Decide where this translation lives (probably a wrapper in `DwfMcpApp.call_tool`).
5. **Concurrency model for scope captures.** A scope capture can take seconds (long acquire window). Should the server block on `asyncio.Lock` during a capture, or surface an "arm + poll" pattern? Probably block for v1 simplicity, but worth thinking through.

## Suggested workflow for stage 2

1. **Brainstorm first** (`/superpowers:brainstorming`) — even though the design covers the tool surface, stage 2 involves real implementation choices (pydwf method names for each instrument, how `ArtifactWriter` integrates, exception mapping) that benefit from a structured run-through. Should be quicker than stage 1's brainstorm since the design is already validated.
2. **Worktree** (`/superpowers:using-git-worktrees`) — create `.worktrees/scope-supply-i2c` (or similar) off `main`.
3. **Plan** (`/superpowers:writing-plans`) — write `docs/plans/2026-06-XX-stage2-implementation.md`.
4. **Execute** (`/superpowers:subagent-driven-development`) — same TDD-per-task model as stage 1.

## Token strategy

The user is on the **Claude Pro plan** (weekly cap). Memory `user-pro-plan-token-caution` already covers this. Recap:

- Commit after every task (no batching) so a mid-stream cutoff is recoverable.
- Use `model: "sonnet"` for routine TDD tasks; reserve Opus for judgment-heavy ones (state machines, integration, real-API verification).
- Check in with the user between tasks rather than blasting through.
- Stage 1 ran 6 Sonnet + 4 Opus tasks with zero review loops needed — the plan-spelled-out-the-test-and-impl approach is what made that possible. Aim for the same level of plan specificity in stage 2.

## Verification baseline (reproducible)

The numbers any new session should expect on a fresh `main` checkout:

- Test count: 37 passed, 1 deselected (hardware)
- Branch tip: `b24312f`
- File count under `src/dwf_mcp/`: 14
- No uncommitted changes in `git status`
