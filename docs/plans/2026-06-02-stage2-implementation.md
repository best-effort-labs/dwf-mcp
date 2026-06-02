# Stage 2 Implementation Plan: scope + supply + i2c

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development from the parent session) to implement this plan task-by-task.

**Goal:** Wire the first three real instruments — `scope`, `supply`, `i2c` — onto the stage 1 foundation as a vertical slice that exercises the architecture against a real AD3.

**Architecture:** Each instrument is an `Instrument` subclass with a `tools` class-level map (suffix → method + JSON Schema). Instruments are lazily instantiated per session, hold their own configuration state, and surface multi-method tool sets. A `DwfDevice.gate_output()` helper centralizes the SafetyPolicy check + safety-log write for any "output goes hot" path. `DwfMcpApp.register_instrument()` walks the tools map and registers `{name}.{suffix}` handlers; `call_tool` wraps dispatch in a try/except that translates known domain exceptions to result-shape errors. Backend ABC gains per-instrument method seams; `FakeBackend` records calls and returns canned data for tests; `PydwfBackend` passes through to pydwf's `AnalogIn`, `AnalogIO`, and `ProtocolI2C`.

**Tech Stack:** Python 3.11+, `mcp`, `pydwf`, `numpy`, `pytest`, `pytest-asyncio`. Same stack as stage 1.

---

## Conventions

- **TDD always.** Test first, then minimal implementation.
- **Commit each task.** Conventional Commit style (`feat:`, `chore:`, `test:`, `refactor:`). Co-author trailer as in stage 1.
- **No hardware in unit tests.** Hardware smoke tests under `tests/hardware/` with `@pytest.mark.hardware`, skipped by default.
- **Type hints everywhere.** `from __future__ import annotations` at the top of every module.
- **No `print`.** Use `logging.getLogger(__name__)`.
- **Run baseline after each task.** `pytest -m "not hardware"`, `ruff check .`, `mypy src/dwf_mcp` — all clean.

## Reference

- Stage 2 design: `docs/plans/2026-06-02-stage2-design.md`
- Overall design: `docs/plans/2026-06-02-dwf-mcp-design.md`
- Handoff (context for why stage 2 looks this way): `docs/plans/2026-06-02-stage2-handoff.md`
- Stage 1 plan (template for style): `docs/plans/2026-06-02-foundation-implementation.md`
- pydwf docs: https://pydwf.readthedocs.io/
- **pydwf method-discovery pattern** (use when in doubt about an API call):
  ```bash
  . .venv/bin/activate && python -c "from pydwf import AnalogIn; print(sorted(m for m in dir(AnalogIn) if not m.startswith('_')))"
  ```

## Worktree + baseline

This plan runs in `/Users/tymm/Documents/claude-code/dwf-mcp/.worktrees/stage2-scope-supply-i2c` (branch `stage2-scope-supply-i2c`). The venv (`.venv/`) is already created and the baseline verified clean: 37 passed, ruff clean, mypy clean.

---

### Task 1: Refactor Instrument ABC to tools-map pattern

**Files:**
- Modify: `src/dwf_mcp/instrument.py`
- Modify: `tests/unit/test_registry.py`

**Background:** Stage 1's ABC has `configure()` + `release()` + `required_pins`. Per the stage 2 design, drop `configure` (supply doesn't have one) and `required_pins` (pins are dynamic per configure args). Add a `tools` class attribute mapping MCP-tool-suffix → `(method_name, input_schema_dict)`. Add `__init__(device, artifacts)` so the registration site can lazily instantiate uniformly. Add `InstrumentNotConfigured` exception for tools like `scope.capture` that require prior `scope.configure`.

**Step 1: Update tests to match new ABC**

Replace `tests/unit/test_registry.py` with:

```python
from __future__ import annotations

from typing import Any, ClassVar

import pytest

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.registry import InstrumentRegistry


class DummyInstrument(Instrument):
    name = "dummy"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "ping": ("ping", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: object, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False

    def ping(self) -> dict[str, str]:
        return {"pong": "ok"}

    def release(self) -> None:
        self._configured = False


def test_register_and_lookup() -> None:
    reg = InstrumentRegistry()
    reg.register(DummyInstrument)
    assert reg.get_class("dummy") is DummyInstrument
    assert "dummy" in reg.names()


def test_duplicate_registration_raises() -> None:
    reg = InstrumentRegistry()
    reg.register(DummyInstrument)
    with pytest.raises(ValueError):
        reg.register(DummyInstrument)


def test_unknown_instrument_raises() -> None:
    reg = InstrumentRegistry()
    with pytest.raises(KeyError):
        reg.get_class("missing")


def test_instrument_abc_requires_name() -> None:
    class Nameless(Instrument):  # type: ignore[misc]
        tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {}

        def __init__(self, device: object, artifacts: ArtifactWriter) -> None: ...
        def release(self) -> None: ...

    reg = InstrumentRegistry()
    with pytest.raises(TypeError):
        reg.register(Nameless)


def test_instrument_not_configured_is_exception() -> None:
    err = InstrumentNotConfigured("scope must be configured before capture")
    assert isinstance(err, Exception)
    assert "scope" in str(err)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_registry.py -v`
Expected: `ImportError: cannot import name 'InstrumentNotConfigured'`.

**Step 3: Rewrite `src/dwf_mcp/instrument.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.device import DwfDevice


class InstrumentNotConfigured(Exception):
    """Raised when a tool is called on an instrument that hasn't been configured."""


class Instrument(ABC):
    name: ClassVar[str]
    # MCP tool suffix -> (method_name_on_instance, input_schema_dict)
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]]

    @abstractmethod
    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None: ...

    @abstractmethod
    def release(self) -> None: ...
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_registry.py -v`
Expected: all 5 tests pass.

Run full suite: `pytest -m "not hardware"`
Expected: 37 passed (same as baseline — only registry tests changed shape).

Run: `ruff check . && mypy src/dwf_mcp`
Expected: clean.

**Step 5: Commit**

```bash
git add src/dwf_mcp/instrument.py tests/unit/test_registry.py
git commit -m "$(cat <<'EOF'
refactor(instrument): tools-map ABC; drop configure/required_pins

Replaces stage 1's configure()+required_pins ABC with a class-level
tools map (suffix -> (method_name, input_schema)). Instruments now
declare their MCP tool surface directly; configure isn't universal
(supply doesn't have one). Adds InstrumentNotConfigured exception
for tools that require prior configuration.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: DwfDevice.gate_output() + safety log

**Files:**
- Modify: `src/dwf_mcp/device.py`
- Modify: `tests/unit/test_device.py` (already exists? check — if not, the existing device tests live in stage 1; locate them and extend)

**Background:** Centralize the "output goes hot" gate on `DwfDevice`. Every instrument that energizes outputs (supply.enable in stage 2; awg.start, pattern.start in stage 3) calls `device.gate_output(kind, **params)`. The helper checks the SafetyPolicy, appends a structured line to `<workspace>/dwf-safety.log`, and raises `SafetyViolation` on rejection.

**Step 1: Locate existing device tests**

Run: `ls tests/unit/test_device.py` — if the file exists from stage 1, append to it. If stage 1 only tested via fake-backend integration, create the file.

Run: `pytest tests/unit/ -v --co 2>&1 | grep test_device | head -10` to find existing tests.

**Step 2: Append failing tests to `tests/unit/test_device.py`**

(If the file doesn't exist, create with the fixtures from stage 1's task 7 plus the cases below.)

Add at the bottom of the file:

```python
import json

from dwf_mcp.policy import SafetyViolation


def test_gate_output_supply_pos_within_cap(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("supply_enable", channel="pos", voltage=3.0)
    log_path = tmp_path / "dwf-safety.log"
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert lines[-1]["kind"] == "supply_enable"
    assert lines[-1]["params"]["voltage"] == 3.0


def test_gate_output_supply_pos_over_cap_raises(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    with pytest.raises(SafetyViolation) as exc:
        device.gate_output("supply_enable", channel="pos", voltage=5.0)
    assert "5.0" in str(exc.value)
    # Rejection is also logged (for audit), with rejected=True
    lines = [json.loads(line) for line in (tmp_path / "dwf-safety.log").read_text().splitlines() if line.strip()]
    assert lines[-1]["rejected"] is True


def test_gate_output_supply_current(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_current=0.5),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("supply_enable", channel="pos", voltage=3.0, current_limit=0.4)
    with pytest.raises(SafetyViolation):
        device.gate_output("supply_enable", channel="pos", voltage=3.0, current_limit=0.6)


def test_gate_output_unknown_kind_passes_through(tmp_path) -> None:
    # Kinds we don't recognize don't get policy checks — they still log.
    # This preserves forward-compat with future kinds added in stage 3.
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )
    device.gate_output("future_kind", foo="bar")
    lines = [json.loads(line) for line in (tmp_path / "dwf-safety.log").read_text().splitlines() if line.strip()]
    assert lines[-1]["kind"] == "future_kind"
```

**Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_device.py -v -k gate_output`
Expected: `AttributeError: 'DwfDevice' object has no attribute 'gate_output'`.

**Step 4: Implement `gate_output` on `DwfDevice`**

Add to `src/dwf_mcp/device.py`:

At the top, add imports:
```python
import json
from datetime import UTC, datetime

from dwf_mcp.policy import SafetyViolation
```

Add this method to `DwfDevice`:

```python
    def gate_output(self, kind: str, **params: Any) -> None:
        """Safety gate for any 'output goes hot' path. Checks policy, writes the safety
        log, raises SafetyViolation on rejection. Rejected attempts are logged too."""
        rejected = False
        rejection_reason: str | None = None
        try:
            self._check_policy(kind, **params)
        except SafetyViolation as exc:
            rejected = True
            rejection_reason = str(exc)
            raise
        finally:
            self._append_safety_log(kind=kind, params=params, rejected=rejected, reason=rejection_reason)

    def _check_policy(self, kind: str, **params: Any) -> None:
        if kind == "supply_enable":
            channel = params.get("channel")
            voltage = params.get("voltage")
            current_limit = params.get("current_limit")
            if isinstance(channel, str) and isinstance(voltage, int | float):
                self.policy.check_supply_voltage(channel, float(voltage))
            if isinstance(current_limit, int | float):
                self.policy.check_supply_current(float(current_limit))
        elif kind == "awg_start":
            amplitude = params.get("amplitude")
            if isinstance(amplitude, int | float):
                self.policy.check_awg_amplitude(float(amplitude))
        # Unknown kinds pass through (forward-compat for stage 3 kinds).

    def _append_safety_log(self, kind: str, params: dict[str, Any], rejected: bool, reason: str | None) -> None:
        path = self.workspace / "dwf-safety.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "params": params,
            "rejected": rejected,
            "reason": reason,
        }
        with path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_device.py -v`
Expected: all device tests pass (old + new 4).

Run full suite: `pytest -m "not hardware"` — expect 37 + 4 = 41 passed.
Run: `ruff check . && mypy src/dwf_mcp` — clean.

**Step 6: Commit**

```bash
git add src/dwf_mcp/device.py tests/unit/test_device.py
git commit -m "$(cat <<'EOF'
feat(device): gate_output safety helper with dwf-safety.log

Centralizes the 'output goes hot' gate: checks the active SafetyPolicy,
appends an audit line to <workspace>/dwf-safety.log, raises
SafetyViolation on rejection. Rejected attempts are logged too with
rejected=true. Stage 2's only caller is supply.enable; stage 3's
awg.start and pattern.start will use the same helper.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: DwfMcpApp.register_instrument + exception mapping + ArtifactWriter

**Files:**
- Modify: `src/dwf_mcp/server.py`
- Modify: `tests/integration/test_server.py`

**Background:** Three intertwined changes to the app:
1. **`register_instrument(cls)`** — walks the `tools` map, instantiates the instrument lazily on first call, registers `{name}.{suffix}` handlers that bind to instance methods.
2. **Exception → result-shape mapping** — translate `SafetyViolation`, `PinAllocationError`, `DwfDeviceLost`, `InstrumentNotConfigured` into `{"error": {"type": ..., "message": ..., "details": ...}}`.
3. **`ArtifactWriter` ownership** — app owns one writer, rebuilds it on `waveforms.open` when `workspace_dir` is provided, syncs `device.workspace` to the writer's workspace so `gate_output` writes the safety log to the same dir.

**Step 1: Append failing tests to `tests/integration/test_server.py`**

```python
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.policy import SafetyViolation


class _Echo(Instrument):
    """Test instrument that exposes a few tool methods covering success and each error kind."""
    name = "echo"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "ping": ("ping", {"type": "object", "properties": {}}),
        "boom_safety": ("boom_safety", {"type": "object", "properties": {}}),
        "boom_unconfigured": ("boom_unconfigured", {"type": "object", "properties": {}}),
    }

    def __init__(self, device: object, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts

    def ping(self) -> dict[str, str]:
        return {"pong": "ok"}

    def boom_safety(self) -> dict[str, Any]:
        raise SafetyViolation("over-voltage")

    def boom_unconfigured(self) -> dict[str, Any]:
        raise InstrumentNotConfigured("must configure first")

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_register_instrument_dispatches_to_method(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("echo.ping", {})
    assert result == {"pong": "ok"}


@pytest.mark.asyncio
async def test_register_instrument_lazy_instantiation(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    assert "echo" not in app.instruments  # not yet created
    await app.call_tool("waveforms.open", {})
    await app.call_tool("echo.ping", {})
    assert "echo" in app.instruments


@pytest.mark.asyncio
async def test_safety_violation_maps_to_error_result(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("echo.boom_safety", {})
    assert result["error"]["type"] == "SafetyViolation"
    assert "over-voltage" in result["error"]["message"]


@pytest.mark.asyncio
async def test_instrument_not_configured_maps_to_error_result(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("echo.boom_unconfigured", {})
    assert result["error"]["type"] == "InstrumentNotConfigured"


@pytest.mark.asyncio
async def test_open_with_workspace_dir_rebuilds_artifacts(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace="")  # default tempdir
    await app.call_tool("waveforms.open", {"workspace_dir": str(tmp_path)})
    assert str(app.artifacts.workspace) == str(tmp_path)
    assert str(app.device.workspace) == str(tmp_path)


@pytest.mark.asyncio
async def test_release_called_on_close(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.register_instrument(_Echo)
    await app.call_tool("waveforms.open", {})
    await app.call_tool("echo.ping", {})  # instantiates echo
    echo = app.instruments["echo"]
    released = {"called": False}
    echo.release = lambda: released.update(called=True)  # type: ignore[method-assign]
    await app.call_tool("waveforms.close", {})
    assert released["called"] is True
    assert app.instruments == {}
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_server.py -v`
Expected: `AttributeError: 'DwfMcpApp' object has no attribute 'register_instrument'`.

**Step 3: Modify `src/dwf_mcp/server.py`**

Add imports near the top (after existing imports):

```python
from dwf_mcp.allocator import PinAllocationError
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backend import DwfDeviceLost
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured
from dwf_mcp.policy import SafetyViolation
```

Add the exception → error-type table near the top of the module (after imports):

```python
_ERROR_TYPES: dict[type[Exception], str] = {
    SafetyViolation: "SafetyViolation",
    PinAllocationError: "PinAllocationError",
    DwfDeviceLost: "DwfDeviceLost",
    InstrumentNotConfigured: "InstrumentNotConfigured",
}
```

Replace the body of `DwfMcpApp` with:

```python
class DwfMcpApp:
    """Holds the device, registry, instruments, and tool dispatch. Tests call `call_tool`
    directly; production wires this up to the MCP SDK stdio transport in `main()`."""

    def __init__(self, device: DwfDevice, registry: InstrumentRegistry) -> None:
        self.device = device
        self.registry = registry
        self.instruments: dict[str, Instrument] = {}
        self.artifacts = ArtifactWriter(workspace=device.workspace if str(device.workspace) else None)
        # Sync device.workspace to whatever ArtifactWriter resolved (covers the temp-dir fallback).
        self.device.workspace = self.artifacts.workspace
        self._tools: dict[str, Any] = {}
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        self._register_meta_tools()

    def _register_meta_tools(self) -> None:
        meta_schema = {"type": "object", "properties": {}}
        for name, handler in [
            ("waveforms.open", self._tool_open),
            ("waveforms.close", self._tool_close),
            ("waveforms.status", self._tool_status),
            ("waveforms.list_pins", self._tool_list_pins),
        ]:
            self._tools[name] = handler
            self._tool_schemas[name] = meta_schema

    def register_instrument(self, cls: type[Instrument]) -> None:
        """Register an instrument class; the instance is created lazily on first tool call.
        Walks cls.tools to register `{instrument.name}.{suffix}` handlers + their schemas."""
        self.registry.register(cls)
        for suffix, (method_name, schema) in cls.tools.items():
            tool_name = f"{cls.name}.{suffix}"
            self._tools[tool_name] = self._make_instrument_handler(cls.name, method_name)
            self._tool_schemas[tool_name] = schema

    def _make_instrument_handler(self, instrument_name: str, method_name: str) -> Any:
        async def handler(**kwargs: Any) -> Any:
            instrument = self._get_or_create_instrument(instrument_name)
            method = getattr(instrument, method_name)
            return method(**kwargs)
        return handler

    def _get_or_create_instrument(self, name: str) -> Instrument:
        if name not in self.instruments:
            cls = self.registry.get_class(name)
            self.instruments[name] = cls(device=self.device, artifacts=self.artifacts)
        return self.instruments[name]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            handler = self._tools[name]
        except KeyError:
            raise ValueError(f"unknown tool {name!r}") from None
        self.device.tick_idle()
        try:
            result = await handler(**args)
            return cast(dict[str, Any], result)
        except tuple(_ERROR_TYPES.keys()) as exc:
            return {
                "error": {
                    "type": _ERROR_TYPES[type(exc)],
                    "message": str(exc),
                    "details": getattr(exc, "details", {}),
                }
            }

    async def _tool_open(self, **kwargs: Any) -> dict[str, Any]:
        policy_fields = {
            f: kwargs.pop(f) for f in [
                "supply_max_voltage_pos", "supply_max_voltage_neg", "supply_max_current",
                "awg_max_amplitude", "pattern_voltage", "require_explicit_enable",
            ] if f in kwargs
        }
        if policy_fields:
            self.device.policy = SafetyPolicy(**policy_fields)
        workspace_dir = kwargs.pop("workspace_dir", None)
        if workspace_dir:
            self.device.workspace = Path(workspace_dir)
            self.artifacts = ArtifactWriter(workspace=self.device.workspace)
        serial = kwargs.pop("device_serial", None)
        info = self.device.open(serial=serial)
        return {
            "device": {
                "serial": info.serial,
                "model": info.model,
                "firmware": info.firmware,
                "sample_rate_max_hz": info.sample_rate_max_hz,
                "dio_count": info.dio_count,
            },
            "workspace": str(self.device.workspace),
        }

    async def _tool_close(self) -> dict[str, Any]:
        for instrument in list(self.instruments.values()):
            instrument.release()
        self.instruments.clear()
        self.device.close()
        return {"closed": True}

    async def _tool_status(self) -> dict[str, Any]:
        return self.device.status()

    async def _tool_list_pins(self) -> dict[str, Any]:
        self.device.require_open()
        return {
            "all_pins": _all_pins(),
            "claimed": self.device.allocator.claimed_pins(),
            "resource_groups": [
                {"name": g.name, "pins": sorted(g.pins), "exclusive": g.exclusive}
                for g in self.device.allocator.resource_groups
            ],
        }
```

Add `Path` to the imports (`from pathlib import Path`).

Update `main()` so `_list_tools` includes schemas:

```python
    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[dict[str, Any]]:
        return [
            {"name": name, "description": "", "inputSchema": app._tool_schemas[name]}  # noqa: SLF001
            for name in app._tools  # noqa: SLF001
        ]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_server.py -v`
Expected: all tests pass (existing 3 + new 6 = 9 total).

Run full suite: `pytest -m "not hardware"` — expect 41 + 6 = 47 passed.
Run: `ruff check . && mypy src/dwf_mcp` — clean.

**Step 5: Commit**

```bash
git add src/dwf_mcp/server.py tests/integration/test_server.py
git commit -m "$(cat <<'EOF'
feat(server): register_instrument + exception mapping + ArtifactWriter

DwfMcpApp now (1) owns an ArtifactWriter and syncs device.workspace to
it, (2) exposes register_instrument(cls) that walks cls.tools and
registers {name}.{suffix} handlers with lazy instantiation, (3) wraps
dispatch in try/except that translates SafetyViolation,
PinAllocationError, DwfDeviceLost, and InstrumentNotConfigured to
result-shape errors so Claude can reason about them. waveforms.close
calls release() on every live instrument.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Extend backend ABC + FakeBackend with scope (AnalogIn) surface

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`
- Modify: `tests/unit/test_fake_backend.py`

**Background:** Each instrument-level operation lives on the `DwfBackend` ABC so both `FakeBackend` and `PydwfBackend` implement the same surface. Scope needs: per-channel configure (enable/range/offset/coupling), acquisition params (sample rate, buffer size, mode), trigger setup, arm, status polling, and per-channel sample read. The fake records calls and returns canned arrays.

**Step 1: Append failing tests to `tests/unit/test_fake_backend.py`**

```python
import numpy as np


def test_scope_methods_record_calls_and_return_canned_data() -> None:
    b = FakeBackend()
    b.open()
    b.scope_configure(channel=1, range_v=5.0, offset_v=0.0, coupling="DC", enable=True)
    b.scope_configure(channel=2, range_v=5.0, offset_v=0.0, coupling="DC", enable=False)
    b.scope_set_acquisition(sample_rate_hz=1_000_000, buffer_size=1024, mode="Single")
    b.scope_set_trigger(source="detector_analog_in", channel=1, level_v=1.0,
                        condition="Rising", position_s=0.0, timeout_s=1.0)

    # Stage a canned capture: 1024 samples on channel 1, sin-ish data.
    samples = np.linspace(-1, 1, 1024, dtype=np.float64)
    b.set_scope_canned_data({1: samples})

    b.scope_arm()
    # Without explicit status progression, fake completes immediately.
    assert b.scope_status() == "Done"
    out = b.scope_read(channel=1, count=1024)
    assert np.array_equal(out, samples)

    # Verify call recording (used by Scope unit tests).
    assert b.scope_calls[0] == ("configure", {"channel": 1, "range_v": 5.0, "offset_v": 0.0, "coupling": "DC", "enable": True})
    kinds = [c[0] for c in b.scope_calls]
    assert "arm" in kinds and "set_acquisition" in kinds


def test_scope_status_progression_can_be_scripted() -> None:
    b = FakeBackend()
    b.open()
    b.set_scope_status_sequence(["Armed", "Triggered", "Done"])
    assert b.scope_status() == "Armed"
    assert b.scope_status() == "Triggered"
    assert b.scope_status() == "Done"
    # After exhausting the sequence, sticks on the last value.
    assert b.scope_status() == "Done"


def test_scope_read_without_canned_returns_zeros() -> None:
    b = FakeBackend()
    b.open()
    out = b.scope_read(channel=1, count=256)
    assert out.shape == (256,)
    assert out.dtype == np.float64
    assert np.all(out == 0.0)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_fake_backend.py -v -k scope`
Expected: `AttributeError: 'FakeBackend' object has no attribute 'scope_configure'`.

**Step 3: Extend `DwfBackend` ABC**

In `src/dwf_mcp/backend.py`, add to the `DwfBackend` class (use `NotImplementedError` rather than `@abstractmethod` so subclasses don't all need to declare every method at once — staged per instrument):

```python
    # Scope (AnalogIn) — added in stage 2.
    def scope_configure(self, channel: int, range_v: float, offset_v: float, coupling: str, enable: bool) -> None:
        raise NotImplementedError

    def scope_set_acquisition(self, sample_rate_hz: float, buffer_size: int, mode: str) -> None:
        raise NotImplementedError

    def scope_set_trigger(self, source: str, channel: int | None, level_v: float,
                          condition: str, position_s: float, timeout_s: float) -> None:
        raise NotImplementedError

    def scope_arm(self) -> None:
        raise NotImplementedError

    def scope_status(self) -> str:
        raise NotImplementedError

    def scope_read(self, channel: int, count: int) -> "np.ndarray[Any, Any]":
        raise NotImplementedError
```

Add at the top:
```python
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    import numpy as np
```

**Step 4: Extend `FakeBackend`**

In `src/dwf_mcp/backends/fake.py`, add imports:
```python
from typing import Any

import numpy as np
```

Modify `FakeBackend.__init__` to add scope state:
```python
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self._devices = devices or [_FAKE_DEVICE]
        self._open_info: DeviceInfo | None = None
        # Scope (AnalogIn) state
        self.scope_calls: list[tuple[str, dict[str, Any]]] = []
        self._scope_canned: dict[int, "np.ndarray[Any, Any]"] = {}
        self._scope_status_sequence: list[str] = ["Done"]
        self._scope_status_idx = 0
```

Add the scope methods to `FakeBackend`:

```python
    # --- Scope (AnalogIn) ---

    def scope_configure(self, channel: int, range_v: float, offset_v: float, coupling: str, enable: bool) -> None:
        self.scope_calls.append(("configure", {
            "channel": channel, "range_v": range_v, "offset_v": offset_v,
            "coupling": coupling, "enable": enable,
        }))

    def scope_set_acquisition(self, sample_rate_hz: float, buffer_size: int, mode: str) -> None:
        self.scope_calls.append(("set_acquisition", {
            "sample_rate_hz": sample_rate_hz, "buffer_size": buffer_size, "mode": mode,
        }))

    def scope_set_trigger(self, source: str, channel: int | None, level_v: float,
                          condition: str, position_s: float, timeout_s: float) -> None:
        self.scope_calls.append(("set_trigger", {
            "source": source, "channel": channel, "level_v": level_v,
            "condition": condition, "position_s": position_s, "timeout_s": timeout_s,
        }))

    def scope_arm(self) -> None:
        self.scope_calls.append(("arm", {}))
        self._scope_status_idx = 0

    def scope_status(self) -> str:
        idx = min(self._scope_status_idx, len(self._scope_status_sequence) - 1)
        result = self._scope_status_sequence[idx]
        self._scope_status_idx += 1
        return result

    def scope_read(self, channel: int, count: int) -> "np.ndarray[Any, Any]":
        if channel in self._scope_canned:
            return self._scope_canned[channel][:count]
        return np.zeros(count, dtype=np.float64)

    # Test helpers
    def set_scope_canned_data(self, channels: dict[int, "np.ndarray[Any, Any]"]) -> None:
        self._scope_canned = dict(channels)

    def set_scope_status_sequence(self, sequence: list[str]) -> None:
        self._scope_status_sequence = list(sequence)
        self._scope_status_idx = 0
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_fake_backend.py -v`
Expected: all (existing 5 + new 3) pass.

Run: `pytest -m "not hardware"` — expect 50 passed.
Run: `ruff check . && mypy src/dwf_mcp` — clean.

**Step 6: Commit**

```bash
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py tests/unit/test_fake_backend.py
git commit -m "$(cat <<'EOF'
feat(backend): scope (AnalogIn) surface on DwfBackend ABC + FakeBackend

Adds the scope-level methods every backend will need: scope_configure,
scope_set_acquisition, scope_set_trigger, scope_arm, scope_status,
scope_read. FakeBackend records calls (scope_calls list) and supports
canned per-channel sample data + scripted status progression so Scope
unit tests can exercise configure->arm->poll->read without hardware.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Scope instrument

**Files:**
- Create: `src/dwf_mcp/instruments/__init__.py`
- Create: `src/dwf_mcp/instruments/scope.py`
- Create: `tests/unit/test_scope.py`
- Modify: `src/dwf_mcp/server.py` (register Scope in `build_app`)
- Modify: `tests/integration/test_server.py`

**Background:** Buffer-mode only for v1. Lifecycle: `configure(channels, range_v, offset_v, coupling, sample_rate_hz, buffer_size)` → `set_trigger(...)` → `capture(output_path?)`. State lives on the instance. `capture` writes the .npz + JSON sidecar via `ArtifactWriter` and returns `{path, sidecar_path, summary}`. The summary contains min/max/mean/rms/freq_estimate/sample_rate/trigger_time per channel.

**Step 1: Write failing unit tests**

Create `tests/unit/test_scope.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.scope import Scope
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def scope(device: DwfDevice, tmp_path: Path) -> Scope:
    device.open()
    return Scope(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_pins_and_records_backend_calls(scope: Scope) -> None:
    scope.configure(channels=[1, 2], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    assert scope.device.allocator.claimed_pins() == {"scope1": "scope", "scope2": "scope"}
    fake = scope.device.backend  # type: ignore[assignment]
    kinds = [c[0] for c in fake.scope_calls]  # type: ignore[attr-defined]
    assert kinds.count("configure") == 2  # both channels
    assert "set_acquisition" in kinds


def test_reconfigure_releases_old_pin_claims(scope: Scope) -> None:
    scope.configure(channels=[1, 2], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    assert scope.device.allocator.claimed_pins() == {"scope1": "scope"}


def test_set_trigger_without_configure_raises(scope: Scope) -> None:
    with pytest.raises(InstrumentNotConfigured):
        scope.set_trigger(source="detector_analog_in", channel=1, level_v=1.0,
                          condition="Rising", position_s=0.0, timeout_s=1.0)


def test_capture_without_configure_raises(scope: Scope) -> None:
    with pytest.raises(InstrumentNotConfigured):
        scope.capture()


def test_capture_returns_path_sidecar_summary(scope: Scope, tmp_path: Path) -> None:
    # Stage canned samples: a 1kHz-ish sine at 1MS/s, 1024 samples.
    t = np.linspace(0, 1024 / 1_000_000, 1024, endpoint=False)
    sine = np.sin(2 * np.pi * 1000 * t)
    scope.device.backend.set_scope_canned_data({1: sine})  # type: ignore[attr-defined]
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    result = scope.capture()
    assert Path(result["path"]).is_file()
    assert Path(result["sidecar_path"]).is_file()
    summary = result["summary"]
    assert "ch1" in summary
    assert abs(summary["ch1"]["min"] - (-1.0)) < 0.01
    assert abs(summary["ch1"]["max"] - 1.0) < 0.01
    assert abs(summary["ch1"]["rms"] - (1 / np.sqrt(2))) < 0.05
    # Freq estimate within 10% (rough zero-crossing).
    assert 900 < summary["ch1"]["freq_estimate"] < 1100
    sidecar = json.loads(Path(result["sidecar_path"]).read_text())
    assert sidecar["config"]["channels"] == [1]


def test_capture_polls_status_until_done(scope: Scope) -> None:
    scope.device.backend.set_scope_status_sequence(  # type: ignore[attr-defined]
        ["Armed", "Armed", "Triggered", "Done"]
    )
    scope.device.backend.set_scope_canned_data({1: np.zeros(1024)})  # type: ignore[attr-defined]
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    result = scope.capture()
    assert "path" in result


def test_release_clears_pin_claims(scope: Scope) -> None:
    scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                    sample_rate_hz=1_000_000, buffer_size=1024)
    scope.release()
    assert scope.device.allocator.claimed_pins() == {}
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_scope.py -v`
Expected: `ModuleNotFoundError: No module named 'dwf_mcp.instruments'`.

**Step 3: Implement Scope**

Create `src/dwf_mcp/instruments/__init__.py`: empty.

Create `src/dwf_mcp/instruments/scope.py`:

```python
"""Scope (analog-in) instrument. Buffer-mode acquisition for v1; streaming deferred."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_VALID_COUPLINGS = {"DC", "AC"}
_VALID_CONDITIONS = {"Rising", "Falling", "Either"}

SCOPE_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channels", "range_v", "sample_rate_hz", "buffer_size"],
    "properties": {
        "channels": {"type": "array", "items": {"type": "integer", "enum": [1, 2]}, "minItems": 1, "uniqueItems": True},
        "range_v": {"type": "number", "minimum": 0.01, "maximum": 50.0},
        "offset_v": {"type": "number", "default": 0.0},
        "coupling": {"type": "string", "enum": ["DC", "AC"], "default": "DC"},
        "sample_rate_hz": {"type": "number", "minimum": 1.0, "maximum": 125_000_000.0},
        "buffer_size": {"type": "integer", "minimum": 16, "maximum": 1_048_576},
    },
}

SCOPE_TRIGGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "properties": {
        "source": {"type": "string", "enum": ["none", "detector_analog_in", "external1", "external2"]},
        "channel": {"type": "integer", "enum": [1, 2]},
        "level_v": {"type": "number", "default": 0.0},
        "condition": {"type": "string", "enum": ["Rising", "Falling", "Either"], "default": "Rising"},
        "position_s": {"type": "number", "default": 0.0},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 1.0},
    },
}

SCOPE_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_path": {"type": "string"},
        "description": {"type": "string"},
    },
}


class Scope(Instrument):
    name = "scope"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":   ("configure",   SCOPE_CONFIGURE_SCHEMA),
        "set_trigger": ("set_trigger", SCOPE_TRIGGER_SCHEMA),
        "capture":     ("capture",     SCOPE_CAPTURE_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._config: dict[str, Any] | None = None
        self._trigger: dict[str, Any] | None = None

    def configure(self, channels: list[int], range_v: float, sample_rate_hz: float,
                  buffer_size: int, offset_v: float = 0.0, coupling: str = "DC") -> dict[str, Any]:
        if coupling not in _VALID_COUPLINGS:
            raise ValueError(f"coupling must be one of {sorted(_VALID_COUPLINGS)}, got {coupling!r}")
        pin_names = [f"scope{c}" for c in channels]
        self.device.allocator.claim("scope", pin_names)
        for ch in (1, 2):
            self.device.backend.scope_configure(
                channel=ch, range_v=range_v, offset_v=offset_v,
                coupling=coupling, enable=(ch in channels),
            )
        self.device.backend.scope_set_acquisition(
            sample_rate_hz=sample_rate_hz, buffer_size=buffer_size, mode="Single",
        )
        self._config = {
            "channels": list(channels), "range_v": range_v, "offset_v": offset_v,
            "coupling": coupling, "sample_rate_hz": sample_rate_hz, "buffer_size": buffer_size,
        }
        return {"configured": True}

    def set_trigger(self, source: str, channel: int | None = None, level_v: float = 0.0,
                    condition: str = "Rising", position_s: float = 0.0,
                    timeout_s: float = 1.0) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("scope.configure must be called before set_trigger")
        if condition not in _VALID_CONDITIONS:
            raise ValueError(f"condition must be one of {sorted(_VALID_CONDITIONS)}, got {condition!r}")
        self.device.backend.scope_set_trigger(
            source=source, channel=channel, level_v=level_v,
            condition=condition, position_s=position_s, timeout_s=timeout_s,
        )
        self._trigger = {
            "source": source, "channel": channel, "level_v": level_v,
            "condition": condition, "position_s": position_s, "timeout_s": timeout_s,
        }
        return {"trigger_set": True}

    def capture(self, output_path: str | None = None, description: str | None = None) -> dict[str, Any]:
        if self._config is None:
            raise InstrumentNotConfigured("scope.configure must be called before capture")
        cfg = self._config
        self.device.backend.scope_arm()
        deadline = time.monotonic() + max(cfg["buffer_size"] / cfg["sample_rate_hz"] * 10 + 1.0, 2.0)
        while time.monotonic() < deadline:
            if self.device.backend.scope_status() == "Done":
                break
        else:
            raise RuntimeError("scope capture did not complete before deadline")

        arrays: dict[str, "np.ndarray[Any, Any]"] = {}
        summary_per_ch: dict[str, dict[str, float]] = {}
        for ch in cfg["channels"]:
            samples = self.device.backend.scope_read(channel=ch, count=cfg["buffer_size"])
            arrays[f"ch{ch}"] = samples
            summary_per_ch[f"ch{ch}"] = self._summarize(samples, cfg["sample_rate_hz"])

        summary = CaptureSummary(
            instrument="scope",
            sample_count=cfg["buffer_size"],
            sample_rate_hz=cfg["sample_rate_hz"],
            extra=summary_per_ch,
        )
        sidecar_config = {**cfg, "trigger": self._trigger}
        result = self.artifacts.write_npz(
            instrument="scope",
            arrays=arrays,
            config=sidecar_config,
            summary=summary,
            output_path=Path(output_path) if output_path else None,
            description=description,
        )
        return {"path": result.path, "sidecar_path": result.sidecar_path, "summary": summary_per_ch}

    def release(self) -> None:
        self.device.allocator.release("scope")
        self._config = None
        self._trigger = None

    @staticmethod
    def _summarize(samples: "np.ndarray[Any, Any]", sample_rate_hz: float) -> dict[str, float]:
        arr = np.asarray(samples, dtype=np.float64)
        mean = float(arr.mean())
        rms = float(np.sqrt(np.mean(arr**2)))
        # Rough frequency estimate via zero-crossings of (arr - mean).
        centered = arr - mean
        signs = np.signbit(centered)
        crossings = int(np.sum(signs[:-1] != signs[1:]))
        freq_estimate = (crossings / 2.0) * (sample_rate_hz / len(arr)) if len(arr) > 0 else 0.0
        return {
            "min": float(arr.min()), "max": float(arr.max()),
            "mean": mean, "rms": rms,
            "freq_estimate": freq_estimate,
            "sample_rate": sample_rate_hz,
        }
```

**Step 4: Register Scope in `build_app`**

In `src/dwf_mcp/server.py`, add an import and a registration call in `build_app` before the `return`:

```python
from dwf_mcp.instruments.scope import Scope
```

And in `build_app`:
```python
    app = DwfMcpApp(device, registry)
    app.register_instrument(Scope)
    return app
```

**Step 5: Append integration test**

Append to `tests/integration/test_server.py`:

```python
@pytest.mark.asyncio
async def test_scope_configure_capture_close_flow(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.backend.set_scope_canned_data(  # type: ignore[attr-defined]
        {1: np.linspace(-1, 1, 512, dtype=np.float64)}
    )
    await app.call_tool("waveforms.open", {})
    cfg = await app.call_tool("scope.configure", {
        "channels": [1], "range_v": 5.0, "sample_rate_hz": 1_000_000, "buffer_size": 512,
    })
    assert cfg == {"configured": True}
    cap = await app.call_tool("scope.capture", {})
    assert "path" in cap
    assert "ch1" in cap["summary"]
    await app.call_tool("waveforms.close", {})


@pytest.mark.asyncio
async def test_scope_capture_before_configure_returns_error(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("scope.capture", {})
    assert result["error"]["type"] == "InstrumentNotConfigured"
```

Add `import numpy as np` near the top of the test file if not already present.

**Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_scope.py tests/integration/test_server.py -v`
Expected: scope tests (7) + new integration (2) pass.

Run: `pytest -m "not hardware"` — expect 50 + 7 + 2 = 59 passed.
Run: `ruff check . && mypy src/dwf_mcp` — clean.

**Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/ src/dwf_mcp/server.py tests/unit/test_scope.py tests/integration/test_server.py
git commit -m "$(cat <<'EOF'
feat(scope): buffer-mode acquisition with configure/trigger/capture

Scope instrument: configure (multi-channel + acquisition params),
set_trigger (source/channel/level/condition/position/timeout), capture
(arm + status poll + per-channel read + summary). Reconfigure releases
prior pin claims atomically. Summary includes min/max/mean/rms/zero-
crossing freq estimate per channel. Writes .npz + JSON sidecar via
ArtifactWriter; tool result is {path, sidecar_path, summary}.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: PydwfBackend AnalogIn + scope hardware smoke

**Files:**
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/hardware/test_scope_hardware.py`

**Background:** Map `DwfBackend.scope_*` methods to `pydwf.AnalogIn` calls on the open device handle. Method names verified in design doc. Hardware smoke test: configure AWG to emit a 1 kHz sine, capture on scope ch1, assert `freq_estimate` is close to 1 kHz.

**Method discovery pattern** (use if any name below proves wrong):
```bash
. .venv/bin/activate && python -c "from pydwf import AnalogIn; print(sorted(m for m in dir(AnalogIn) if not m.startswith('_')))"
```

**Step 1: Extend PydwfBackend**

In `src/dwf_mcp/backends/pydwf_backend.py`, add imports:
```python
import time

import numpy as np
```

Add to the imports block:
```python
from pydwf import (  # type: ignore[import-untyped]
    DwfAcquisitionMode,
    DwfAnalogCoupling,
    DwfTriggerSlope,
    DwfTriggerSource,
)
```

Then add methods to `PydwfBackend`. The pattern is: every method goes through `self._analog_in` which we resolve lazily from `self._device.analogIn`:

```python
    @property
    def _analog_in(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.analogIn

    def scope_configure(self, channel: int, range_v: float, offset_v: float, coupling: str, enable: bool) -> None:
        ch_idx = channel - 1  # pydwf is 0-indexed
        ain = self._analog_in
        ain.channelEnableSet(ch_idx, enable)
        if enable:
            ain.channelRangeSet(ch_idx, range_v)
            ain.channelOffsetSet(ch_idx, offset_v)
            cp = DwfAnalogCoupling.DC if coupling == "DC" else DwfAnalogCoupling.AC
            ain.channelCouplingSet(ch_idx, cp)

    def scope_set_acquisition(self, sample_rate_hz: float, buffer_size: int, mode: str) -> None:
        ain = self._analog_in
        ain.frequencySet(sample_rate_hz)
        ain.bufferSizeSet(buffer_size)
        # Only "Single" supported in v1. Streaming is stage 3.
        if mode != "Single":
            raise ValueError(f"only Single mode supported in v1, got {mode!r}")
        ain.acquisitionModeSet(DwfAcquisitionMode.Single)

    def scope_set_trigger(self, source: str, channel: int | None, level_v: float,
                          condition: str, position_s: float, timeout_s: float) -> None:
        ain = self._analog_in
        src_map = {
            "none": DwfTriggerSource.None_,
            "detector_analog_in": DwfTriggerSource.DetectorAnalogIn,
            "external1": DwfTriggerSource.External1,
            "external2": DwfTriggerSource.External2,
        }
        ain.triggerSourceSet(src_map[source])
        if channel is not None:
            ain.triggerChannelSet(channel - 1)
        ain.triggerLevelSet(level_v)
        slope = DwfTriggerSlope.Rise if condition == "Rising" else (
            DwfTriggerSlope.Fall if condition == "Falling" else DwfTriggerSlope.Either)
        ain.triggerConditionSet(slope)
        ain.triggerPositionSet(position_s)
        ain.triggerAutoTimeoutSet(timeout_s)

    def scope_arm(self) -> None:
        self._analog_in.configure(False, True)  # reconfigure=False, start=True

    def scope_status(self) -> str:
        from pydwf import DwfState  # type: ignore[import-untyped]
        st = self._analog_in.status(True)  # readData=True
        # Map DwfState enum to our string. We care about "Done"; map the rest as their name.
        if st == DwfState.Done:
            return "Done"
        return getattr(st, "name", str(st))

    def scope_read(self, channel: int, count: int) -> "np.ndarray[Any, Any]":
        return np.asarray(self._analog_in.statusData(channel - 1, count), dtype=np.float64)
```

**Step 2: Write hardware smoke test**

Create `tests/hardware/test_scope_hardware.py`:

```python
"""Hardware smoke test for scope. Requires AD3 with W1 wired to scope ch1+ (or via signal generator).

Run: pytest tests/hardware/test_scope_hardware.py -m hardware -v
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
def test_scope_captures_1khz_sine_from_awg(tmp_path) -> None:
    """Start AWG ch1 at 1 kHz sine, capture on scope ch1, assert freq estimate near 1 kHz.

    Requires: W1 wired to scope ch1+ (or AD3 internal loopback if available).
    """
    pytest.importorskip("pydwf")
    from pydwf import DwfAnalogOutFunction, DwfAnalogOutNode  # type: ignore[import-untyped]

    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.scope import Scope
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend, policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    try:
        # Drive AWG ch1 = 1 kHz sine, 1 Vpp, via raw pydwf (AWG instrument not yet wired).
        ao = backend._device.analogOut  # type: ignore[attr-defined]
        ao.nodeEnableSet(0, DwfAnalogOutNode.Carrier, True)
        ao.nodeFunctionSet(0, DwfAnalogOutNode.Carrier, DwfAnalogOutFunction.Sine)
        ao.nodeFrequencySet(0, DwfAnalogOutNode.Carrier, 1000.0)
        ao.nodeAmplitudeSet(0, DwfAnalogOutNode.Carrier, 1.0)
        ao.configure(0, True)

        scope = Scope(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
        scope.configure(channels=[1], range_v=5.0, offset_v=0.0, coupling="DC",
                        sample_rate_hz=100_000, buffer_size=4096)
        scope.set_trigger(source="detector_analog_in", channel=1, level_v=0.0,
                          condition="Rising", position_s=0.0, timeout_s=1.0)
        result = scope.capture()
        freq = result["summary"]["ch1"]["freq_estimate"]
        assert 900 < freq < 1100, f"expected ~1000 Hz, got {freq}"
    finally:
        device.close()
```

**Step 3: Run unit suite to confirm no regressions**

Run: `pytest -m "not hardware" -v`
Expected: still 59 passed. `ruff check . && mypy src/dwf_mcp` — clean.

**Step 4: Run hardware smoke (if AD3 connected)**

Run: `pytest tests/hardware/test_scope_hardware.py -m hardware -v`
Expected on a connected AD3 with the loopback wired: 1 passed. If pydwf method names differ, use the discovery pattern and fix.

If method names are wrong, the fix loop is:
1. Activate venv, introspect the actual class
2. Update the method call in `pydwf_backend.py`
3. Re-run hardware test

**Step 5: Commit**

```bash
git add src/dwf_mcp/backends/pydwf_backend.py tests/hardware/test_scope_hardware.py
git commit -m "$(cat <<'EOF'
feat(backend): pydwf AnalogIn passthroughs + scope hardware smoke

Wires scope_configure/set_acquisition/set_trigger/arm/status/read to
pydwf.AnalogIn on the live device handle. Hardware smoke drives the
AWG at 1 kHz sine and asserts the scope's zero-crossing frequency
estimate lands within 100 Hz. Skipped by default; run with
'-m hardware' against a connected AD3 with W1 wired to scope ch1+.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Extend backend ABC + FakeBackend with supply (AnalogIO) surface

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`
- Modify: `tests/unit/test_fake_backend.py`

**Background:** AD3's supplies live on AnalogIO channels with per-rail nodes (enable/voltage/current). The backend exposes `supply_discover_nodes() -> {rail_name: (channel_idx, {node_name: node_idx})}` for the Supply instrument to resolve indices at init. Plus per-(channel,node) set/get and a master enable.

**Step 1: Append failing tests to `tests/unit/test_fake_backend.py`**

```python
def test_supply_discover_returns_canned_layout() -> None:
    b = FakeBackend()
    b.open()
    layout = b.supply_discover_nodes()
    # Default canned layout exposes vpos and vneg, each with enable/voltage/current nodes.
    assert set(layout.keys()) == {"vpos", "vneg"}
    pos_ch, pos_nodes = layout["vpos"]
    assert {"enable", "voltage", "current"} <= set(pos_nodes.keys())


def test_supply_set_and_get_node_roundtrip() -> None:
    b = FakeBackend()
    b.open()
    layout = b.supply_discover_nodes()
    ch, nodes = layout["vpos"]
    b.supply_node_set(ch, nodes["voltage"], 2.5)
    # In fake, get returns what was last set (or canned measured value if scripted).
    assert b.supply_node_get(ch, nodes["voltage"]) == 2.5


def test_supply_master_enable_records() -> None:
    b = FakeBackend()
    b.open()
    b.supply_master_enable(True)
    b.supply_master_enable(False)
    enables = [c for c in b.supply_calls if c[0] == "master_enable"]
    assert [c[1]["enabled"] for c in enables] == [True, False]


def test_supply_canned_measurement_overrides_setpoint() -> None:
    b = FakeBackend()
    b.open()
    layout = b.supply_discover_nodes()
    ch, nodes = layout["vpos"]
    b.set_supply_canned_status({(ch, nodes["voltage"]): 1.97})
    b.supply_node_set(ch, nodes["voltage"], 2.0)
    assert b.supply_node_get(ch, nodes["voltage"]) == 1.97
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_fake_backend.py -v -k supply`
Expected: AttributeError on `supply_discover_nodes`.

**Step 3: Extend DwfBackend ABC**

In `src/dwf_mcp/backend.py`, add to `DwfBackend`:

```python
    # Supply (AnalogIO) — added in stage 2.
    def supply_discover_nodes(self) -> dict[str, tuple[int, dict[str, int]]]:
        raise NotImplementedError

    def supply_node_set(self, channel: int, node: int, value: float) -> None:
        raise NotImplementedError

    def supply_node_get(self, channel: int, node: int) -> float:
        raise NotImplementedError

    def supply_master_enable(self, enabled: bool) -> None:
        raise NotImplementedError
```

**Step 4: Extend FakeBackend**

In `src/dwf_mcp/backends/fake.py`, extend `__init__`:

```python
        # Supply (AnalogIO) state
        self.supply_calls: list[tuple[str, dict[str, Any]]] = []
        self._supply_layout: dict[str, tuple[int, dict[str, int]]] = {
            "vpos": (0, {"enable": 0, "voltage": 1, "current": 2}),
            "vneg": (1, {"enable": 0, "voltage": 1, "current": 2}),
        }
        self._supply_setpoints: dict[tuple[int, int], float] = {}
        self._supply_canned_status: dict[tuple[int, int], float] = {}
```

Add methods:

```python
    # --- Supply (AnalogIO) ---

    def supply_discover_nodes(self) -> dict[str, tuple[int, dict[str, int]]]:
        return {k: (ch, dict(nodes)) for k, (ch, nodes) in self._supply_layout.items()}

    def supply_node_set(self, channel: int, node: int, value: float) -> None:
        self._supply_setpoints[(channel, node)] = value
        self.supply_calls.append(("node_set", {"channel": channel, "node": node, "value": value}))

    def supply_node_get(self, channel: int, node: int) -> float:
        if (channel, node) in self._supply_canned_status:
            return self._supply_canned_status[(channel, node)]
        return self._supply_setpoints.get((channel, node), 0.0)

    def supply_master_enable(self, enabled: bool) -> None:
        self.supply_calls.append(("master_enable", {"enabled": enabled}))

    # Test helpers
    def set_supply_canned_status(self, values: dict[tuple[int, int], float]) -> None:
        self._supply_canned_status = dict(values)
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_fake_backend.py -v`
Expected: all pass (existing + 4 new).

Run: `pytest -m "not hardware"` — expect 63 passed. `ruff check . && mypy src/dwf_mcp` — clean.

**Step 6: Commit**

```bash
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py tests/unit/test_fake_backend.py
git commit -m "$(cat <<'EOF'
feat(backend): supply (AnalogIO) surface on ABC + FakeBackend

Adds supply_discover_nodes (returns rail -> (channel, {node_name:
node_idx}) so Supply instruments resolve indices at init),
supply_node_set/get for voltage/current/enable per node, and
supply_master_enable for the device-wide gate. Fake exposes a canned
layout for vpos/vneg, records calls, and supports overriding read-back
values via set_supply_canned_status for testing measurement drift.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Supply instrument with safety gate

**Files:**
- Create: `src/dwf_mcp/instruments/supply.py`
- Create: `tests/unit/test_supply.py`
- Modify: `src/dwf_mcp/server.py` (register Supply)
- Modify: `tests/integration/test_server.py`

**Background:** Supply discovers its (channel, node) indices at `__init__`. `set(channel, voltage, current_limit?)` stores requested values + writes voltage/current setpoints but does NOT energize. `enable(channel)` calls `device.gate_output("supply_enable", channel="pos"|"neg", voltage=..., current_limit=...)`, which raises `SafetyViolation` if the policy rejects. On success, sets the per-channel enable node + master enable. `disable(channel)` clears the per-rail enable and drops master enable when no rails remain on. `read(channel)` returns `{requested: {voltage, current_limit}, measured: {voltage, current}}`.

Note: pin allocator gets `[rail_name]` claims (e.g. `"vpos"`).

**Step 1: Write failing tests**

Create `tests/unit/test_supply.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instruments.supply import Supply
from dwf_mcp.policy import SafetyPolicy, SafetyViolation


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(supply_max_voltage_pos=3.3, supply_max_voltage_neg=-3.3, supply_max_current=0.5),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def supply(device: DwfDevice, tmp_path: Path) -> Supply:
    device.open()
    return Supply(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_set_stores_voltage_does_not_energize(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    fake = supply.device.backend  # type: ignore[assignment]
    # No master_enable call yet.
    enables = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    assert enables == []
    # Voltage and current_limit were written to the setpoint nodes.
    kinds = [c for c in fake.supply_calls if c[0] == "node_set"]  # type: ignore[attr-defined]
    assert any(c[1]["value"] == 3.0 for c in kinds)
    assert any(c[1]["value"] == 0.4 for c in kinds)


def test_set_claims_pin(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0)
    assert supply.device.allocator.claimed_pins() == {"vpos": "supply"}


def test_enable_above_cap_raises_safety_violation(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=5.0)  # set itself doesn't check
    with pytest.raises(SafetyViolation):
        supply.enable(channel="vpos")


def test_enable_within_cap_calls_master_enable(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    masters = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    assert masters[-1][1] == {"enabled": True}


def test_enable_without_set_raises_instrument_not_configured(supply: Supply) -> None:
    from dwf_mcp.instrument import InstrumentNotConfigured
    with pytest.raises(InstrumentNotConfigured):
        supply.enable(channel="vpos")


def test_disable_drops_master_when_no_rails_remain_on(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    supply.disable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    masters = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    # Sequence: True (on enable), False (on disable since no rails left).
    assert [m[1]["enabled"] for m in masters] == [True, False]


def test_disable_keeps_master_on_when_other_rail_still_on(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.set(channel="vneg", voltage=-3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    supply.enable(channel="vneg")
    supply.disable(channel="vpos")
    fake = supply.device.backend  # type: ignore[assignment]
    masters = [c for c in fake.supply_calls if c[0] == "master_enable"]  # type: ignore[attr-defined]
    # True, True, no second False.
    assert masters[-1][1]["enabled"] is True


def test_read_returns_requested_and_measured(supply: Supply) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    # Override the measured voltage to simulate slight drift.
    layout = supply.device.backend.supply_discover_nodes()  # type: ignore[attr-defined]
    ch, nodes = layout["vpos"]
    supply.device.backend.set_supply_canned_status(  # type: ignore[attr-defined]
        {(ch, nodes["voltage"]): 2.97, (ch, nodes["current"]): 0.001}
    )
    state = supply.read(channel="vpos")
    assert state["requested"]["voltage"] == 3.0
    assert state["measured"]["voltage"] == 2.97
    assert state["measured"]["current"] == 0.001


def test_safety_log_records_supply_enable(supply: Supply, tmp_path: Path) -> None:
    supply.set(channel="vpos", voltage=3.0, current_limit=0.4)
    supply.enable(channel="vpos")
    import json
    lines = [
        json.loads(line)
        for line in (supply.device.workspace / "dwf-safety.log").read_text().splitlines()
        if line.strip()
    ]
    assert lines[-1]["kind"] == "supply_enable"
    assert lines[-1]["params"]["voltage"] == 3.0
    assert lines[-1]["rejected"] is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_supply.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement Supply**

Create `src/dwf_mcp/instruments/supply.py`:

```python
"""Supply (AnalogIO) instrument. Safety-gated programmable rails: vpos / vneg."""
from __future__ import annotations

from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_CHANNEL_TO_POLICY_KIND = {"vpos": "pos", "vneg": "neg"}

SUPPLY_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel", "voltage"],
    "properties": {
        "channel": {"type": "string", "enum": ["vpos", "vneg"]},
        "voltage": {"type": "number"},
        "current_limit": {"type": "number", "minimum": 0.0},
    },
}

SUPPLY_CHANNEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["channel"],
    "properties": {"channel": {"type": "string", "enum": ["vpos", "vneg"]}},
}


class Supply(Instrument):
    name = "supply"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "set":     ("set",     SUPPLY_SET_SCHEMA),
        "enable":  ("enable",  SUPPLY_CHANNEL_SCHEMA),
        "disable": ("disable", SUPPLY_CHANNEL_SCHEMA),
        "read":    ("read",    SUPPLY_CHANNEL_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._layout = device.backend.supply_discover_nodes()
        self._setpoints: dict[str, dict[str, float]] = {}  # channel -> {voltage, current_limit}
        self._enabled: set[str] = set()

    def set(self, channel: str, voltage: float, current_limit: float | None = None) -> dict[str, Any]:
        if channel not in self._layout:
            raise ValueError(f"unknown supply channel {channel!r}; have {sorted(self._layout)}")
        ch_idx, nodes = self._layout[channel]
        self.device.allocator.claim("supply", list({*self.device.allocator.claimed_pins().keys(), channel}
                                                   if False else self._claimed_channels() | {channel}))
        self.device.backend.supply_node_set(ch_idx, nodes["voltage"], voltage)
        if current_limit is not None:
            self.device.backend.supply_node_set(ch_idx, nodes["current"], current_limit)
        self._setpoints[channel] = {
            "voltage": voltage,
            "current_limit": current_limit if current_limit is not None else self._setpoints.get(channel, {}).get("current_limit", 0.0),
        }
        return {"set": True, "channel": channel, "voltage": voltage, "current_limit": current_limit}

    def enable(self, channel: str) -> dict[str, Any]:
        if channel not in self._setpoints:
            raise InstrumentNotConfigured(f"supply.set must be called for {channel!r} before enable")
        ch_idx, nodes = self._layout[channel]
        sp = self._setpoints[channel]
        # Safety gate — raises SafetyViolation on rejection (also logs).
        self.device.gate_output(
            "supply_enable",
            channel=_CHANNEL_TO_POLICY_KIND[channel],
            voltage=sp["voltage"],
            current_limit=sp["current_limit"],
        )
        self.device.backend.supply_node_set(ch_idx, nodes["enable"], 1.0)
        self._enabled.add(channel)
        self.device.backend.supply_master_enable(True)
        return {"enabled": True, "channel": channel}

    def disable(self, channel: str) -> dict[str, Any]:
        if channel not in self._layout:
            raise ValueError(f"unknown supply channel {channel!r}")
        ch_idx, nodes = self._layout[channel]
        self.device.backend.supply_node_set(ch_idx, nodes["enable"], 0.0)
        self._enabled.discard(channel)
        if not self._enabled:
            self.device.backend.supply_master_enable(False)
        return {"disabled": True, "channel": channel}

    def read(self, channel: str) -> dict[str, Any]:
        if channel not in self._layout:
            raise ValueError(f"unknown supply channel {channel!r}")
        ch_idx, nodes = self._layout[channel]
        measured_v = self.device.backend.supply_node_get(ch_idx, nodes["voltage"])
        measured_i = self.device.backend.supply_node_get(ch_idx, nodes["current"])
        requested = self._setpoints.get(channel, {"voltage": 0.0, "current_limit": 0.0})
        return {
            "channel": channel,
            "requested": requested,
            "measured": {"voltage": measured_v, "current": measured_i},
            "enabled": channel in self._enabled,
        }

    def release(self) -> None:
        for ch in list(self._enabled):
            try:
                self.disable(ch)
            except Exception:
                pass
        self.device.allocator.release("supply")
        self._setpoints.clear()
        self._enabled.clear()

    def _claimed_channels(self) -> set[str]:
        claims = self.device.allocator.claimed_pins()
        return {pin for pin, instr in claims.items() if instr == "supply"}
```

Note the awkward expression in `set` for pin claims — clean it up:

Replace this:
```python
        self.device.allocator.claim("supply", list({*self.device.allocator.claimed_pins().keys(), channel}
                                                   if False else self._claimed_channels() | {channel}))
```
with:
```python
        self.device.allocator.claim("supply", sorted(self._claimed_channels() | {channel}))
```

**Step 4: Register Supply in `build_app`**

In `src/dwf_mcp/server.py`:
```python
from dwf_mcp.instruments.supply import Supply
```

And:
```python
    app.register_instrument(Supply)
```
(right after `app.register_instrument(Scope)`)

**Step 5: Append integration test**

In `tests/integration/test_server.py`:

```python
@pytest.mark.asyncio
async def test_supply_set_enable_read_disable_flow(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 3.3})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 3.0, "current_limit": 0.4})
    enable_result = await app.call_tool("supply.enable", {"channel": "vpos"})
    assert enable_result == {"enabled": True, "channel": "vpos"}
    read_result = await app.call_tool("supply.read", {"channel": "vpos"})
    assert read_result["enabled"] is True
    assert read_result["requested"]["voltage"] == 3.0
    await app.call_tool("supply.disable", {"channel": "vpos"})
    await app.call_tool("waveforms.close", {})


@pytest.mark.asyncio
async def test_supply_enable_above_cap_returns_safety_error(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 3.3})
    await app.call_tool("supply.set", {"channel": "vpos", "voltage": 5.0})
    result = await app.call_tool("supply.enable", {"channel": "vpos"})
    assert result["error"]["type"] == "SafetyViolation"
    assert "5.0" in result["error"]["message"]
```

**Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_supply.py tests/integration/test_server.py -v`
Expected: supply tests (9) + integration (2) pass.

Run: `pytest -m "not hardware"` — expect 63 + 9 + 2 = 74 passed.
Run: `ruff check . && mypy src/dwf_mcp` — clean.

**Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/supply.py src/dwf_mcp/server.py tests/unit/test_supply.py tests/integration/test_server.py
git commit -m "$(cat <<'EOF'
feat(supply): safety-gated programmable rails (vpos/vneg)

Supply instrument: set writes voltage/current setpoints but does NOT
energize; enable routes through device.gate_output (which checks the
SafetyPolicy and writes to dwf-safety.log) before flipping the
per-rail and master enable nodes; disable drops master only when no
rails remain on; read returns requested+measured. Pin claims track
active rails so reconfigure releases atomically.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: PydwfBackend AnalogIO + supply hardware smoke

**Files:**
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/hardware/test_supply_hardware.py`

**Background:** Map supply methods to pydwf.AnalogIO. Discovery walks `channelCount` × `channelNodeCount`, matches names against expected rail/node strings (case-insensitive). Hardware smoke: set vpos=1.0V, enable, read measured, disable, assert read ≈ 0.

**Step 1: Extend PydwfBackend**

In `src/dwf_mcp/backends/pydwf_backend.py`, add:

```python
    @property
    def _analog_io(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.analogIO

    def supply_discover_nodes(self) -> dict[str, tuple[int, dict[str, int]]]:
        aio = self._analog_io
        aio.reset()
        layout: dict[str, tuple[int, dict[str, int]]] = {}
        ch_count = aio.channelCount()
        for ch_idx in range(ch_count):
            ch_name = aio.channelName(ch_idx)[0].lower()  # returns (label, info) per pydwf docs
            # Map AD3 supply channel labels to our rail names.
            rail: str | None = None
            if "v+" in ch_name or "positive" in ch_name or "vpos" in ch_name:
                rail = "vpos"
            elif "v-" in ch_name or "negative" in ch_name or "vneg" in ch_name:
                rail = "vneg"
            if rail is None:
                continue
            node_count = aio.channelInfo(ch_idx)
            nodes: dict[str, int] = {}
            for node_idx in range(node_count):
                node_name = aio.channelNodeName(ch_idx, node_idx)[0].lower()
                if "enable" in node_name:
                    nodes["enable"] = node_idx
                elif "voltage" in node_name:
                    nodes["voltage"] = node_idx
                elif "current" in node_name:
                    nodes["current"] = node_idx
            if {"enable", "voltage"} <= set(nodes.keys()):
                layout[rail] = (ch_idx, nodes)
        if not layout:
            raise DwfBackendError("could not discover supply layout on AnalogIO")
        return layout

    def supply_node_set(self, channel: int, node: int, value: float) -> None:
        self._analog_io.channelNodeSet(channel, node, value)

    def supply_node_get(self, channel: int, node: int) -> float:
        aio = self._analog_io
        aio.status()  # refresh
        return float(aio.channelNodeStatus(channel, node))

    def supply_master_enable(self, enabled: bool) -> None:
        self._analog_io.enableSet(enabled)
        self._analog_io.configure()
```

**Note:** If `channelName`/`channelInfo`/`channelNodeName` return shapes differ from `(label, info)` tuples, adjust unpacking — pydwf has historically returned strings directly in some versions. Use the discovery pattern to verify:

```bash
. .venv/bin/activate && python -c "from pydwf import AnalogIO; help(AnalogIO.channelName)"
```

**Step 2: Write hardware smoke test**

Create `tests/hardware/test_supply_hardware.py`:

```python
from __future__ import annotations

import time

import pytest


@pytest.mark.hardware
def test_supply_vpos_round_trip(tmp_path) -> None:
    """Enable vpos at 1.0 V, read back, disable, read back ~0 V. Requires AD3."""
    pytest.importorskip("pydwf")
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.supply import Supply
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(supply_max_voltage_pos=3.3),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    try:
        supply = Supply(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
        supply.set(channel="vpos", voltage=1.0, current_limit=0.1)
        supply.enable(channel="vpos")
        time.sleep(0.2)  # let the rail settle
        state = supply.read(channel="vpos")
        assert 0.9 < state["measured"]["voltage"] < 1.1, state
        supply.disable(channel="vpos")
        time.sleep(0.2)
        state = supply.read(channel="vpos")
        assert state["measured"]["voltage"] < 0.2, state
    finally:
        device.close()
```

**Step 3: Run unit suite**

Run: `pytest -m "not hardware" -v` — expect 74 passed. `ruff check . && mypy src/dwf_mcp` — clean.

**Step 4: Run hardware smoke (if AD3 connected)**

Run: `pytest tests/hardware/test_supply_hardware.py -m hardware -v`

Adjust pydwf calls if needed.

**Step 5: Commit**

```bash
git add src/dwf_mcp/backends/pydwf_backend.py tests/hardware/test_supply_hardware.py
git commit -m "$(cat <<'EOF'
feat(backend): pydwf AnalogIO passthroughs + supply hardware smoke

Wires supply_discover_nodes/node_set/node_get/master_enable to
pydwf.AnalogIO. Discovery walks channels and nodes, matching labels
against vpos/vneg/enable/voltage/current — robust to AD2/AD3 layout
differences. Hardware smoke enables vpos at 1.0 V, reads back within
100 mV, disables, asserts measured drops below 200 mV.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Extend backend ABC + FakeBackend with i2c (ProtocolI2C) surface

**Files:**
- Modify: `src/dwf_mcp/backend.py`
- Modify: `src/dwf_mcp/backends/fake.py`
- Modify: `tests/unit/test_fake_backend.py`

**Background:** I2C surface mirrors pydwf.ProtocolI2C: configure (sda/scl pin indices, rate, stretch, timeout), reset, write (returns nak count), read (returns bytes), write_read, write_one (for scan). Fake records calls and returns canned NAK/byte responses keyed by address.

**Step 1: Append failing tests to `tests/unit/test_fake_backend.py`**

```python
def test_i2c_configure_and_reset_record() -> None:
    b = FakeBackend()
    b.open()
    b.i2c_configure(scl_pin_idx=0, sda_pin_idx=1, rate_hz=100_000, stretch=True, timeout_s=0.1)
    b.i2c_reset()
    kinds = [c[0] for c in b.i2c_calls]
    assert kinds == ["configure", "reset"]


def test_i2c_write_returns_canned_nak() -> None:
    b = FakeBackend()
    b.open()
    b.set_i2c_acks({0x50: True, 0x51: False})  # 0x50 ACKs, 0x51 NAKs
    b.i2c_configure(scl_pin_idx=0, sda_pin_idx=1, rate_hz=100_000, stretch=True, timeout_s=0.1)
    assert b.i2c_write(address=0x50, data=b"\x00") == 0  # acked
    assert b.i2c_write(address=0x51, data=b"\x00") == 1  # naked


def test_i2c_read_returns_canned_bytes() -> None:
    b = FakeBackend()
    b.open()
    b.set_i2c_reads({0x50: b"\xde\xad\xbe\xef"})
    b.i2c_configure(scl_pin_idx=0, sda_pin_idx=1, rate_hz=100_000, stretch=True, timeout_s=0.1)
    assert b.i2c_read(address=0x50, length=4) == b"\xde\xad\xbe\xef"
    assert b.i2c_read(address=0x50, length=2) == b"\xde\xad"


def test_i2c_write_read_roundtrip() -> None:
    b = FakeBackend()
    b.open()
    b.set_i2c_reads({0x50: b"\x01\x02"})
    b.i2c_configure(scl_pin_idx=0, sda_pin_idx=1, rate_hz=100_000, stretch=True, timeout_s=0.1)
    assert b.i2c_write_read(address=0x50, write_data=b"\x10", read_length=2) == b"\x01\x02"


def test_i2c_write_one_used_for_scan() -> None:
    b = FakeBackend()
    b.open()
    b.set_i2c_acks({0x50: True, 0x51: True})
    b.i2c_configure(scl_pin_idx=0, sda_pin_idx=1, rate_hz=100_000, stretch=True, timeout_s=0.1)
    assert b.i2c_write_one(address=0x50, byte=0) == 0
    assert b.i2c_write_one(address=0x77, byte=0) == 1  # not in canned -> NAK
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_fake_backend.py -v -k i2c`
Expected: AttributeError.

**Step 3: Extend DwfBackend ABC**

In `src/dwf_mcp/backend.py`, add:

```python
    # I2C (ProtocolI2C) — added in stage 2.
    def i2c_configure(self, scl_pin_idx: int, sda_pin_idx: int, rate_hz: float,
                      stretch: bool, timeout_s: float) -> None:
        raise NotImplementedError

    def i2c_reset(self) -> None:
        raise NotImplementedError

    def i2c_write(self, address: int, data: bytes) -> int:
        raise NotImplementedError

    def i2c_read(self, address: int, length: int) -> bytes:
        raise NotImplementedError

    def i2c_write_read(self, address: int, write_data: bytes, read_length: int) -> bytes:
        raise NotImplementedError

    def i2c_write_one(self, address: int, byte: int) -> int:
        raise NotImplementedError
```

**Step 4: Extend FakeBackend**

Add to `__init__`:

```python
        # I2C (ProtocolI2C) state
        self.i2c_calls: list[tuple[str, dict[str, Any]]] = []
        self._i2c_acks: dict[int, bool] = {}
        self._i2c_reads: dict[int, bytes] = {}
```

Add methods:

```python
    # --- I2C (ProtocolI2C) ---

    def i2c_configure(self, scl_pin_idx: int, sda_pin_idx: int, rate_hz: float,
                      stretch: bool, timeout_s: float) -> None:
        self.i2c_calls.append(("configure", {
            "scl_pin_idx": scl_pin_idx, "sda_pin_idx": sda_pin_idx,
            "rate_hz": rate_hz, "stretch": stretch, "timeout_s": timeout_s,
        }))

    def i2c_reset(self) -> None:
        self.i2c_calls.append(("reset", {}))

    def i2c_write(self, address: int, data: bytes) -> int:
        self.i2c_calls.append(("write", {"address": address, "data": data}))
        return 0 if self._i2c_acks.get(address, False) else 1

    def i2c_read(self, address: int, length: int) -> bytes:
        self.i2c_calls.append(("read", {"address": address, "length": length}))
        canned = self._i2c_reads.get(address, b"")
        return canned[:length]

    def i2c_write_read(self, address: int, write_data: bytes, read_length: int) -> bytes:
        self.i2c_calls.append(("write_read", {
            "address": address, "write_data": write_data, "read_length": read_length,
        }))
        return self._i2c_reads.get(address, b"")[:read_length]

    def i2c_write_one(self, address: int, byte: int) -> int:
        self.i2c_calls.append(("write_one", {"address": address, "byte": byte}))
        return 0 if self._i2c_acks.get(address, False) else 1

    # Test helpers
    def set_i2c_acks(self, acks: dict[int, bool]) -> None:
        self._i2c_acks = dict(acks)

    def set_i2c_reads(self, reads: dict[int, bytes]) -> None:
        self._i2c_reads = dict(reads)
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_fake_backend.py -v` — all pass.
Run: `pytest -m "not hardware"` — expect 79 passed. `ruff check . && mypy src/dwf_mcp` — clean.

**Step 6: Commit**

```bash
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/fake.py tests/unit/test_fake_backend.py
git commit -m "$(cat <<'EOF'
feat(backend): i2c (ProtocolI2C) surface on ABC + FakeBackend

Adds i2c_configure/reset/write/read/write_read/write_one. Fake records
calls and supports canned ACK/NAK + read responses keyed by address —
write_one is what i2c.scan walks the address range with.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: I2C instrument

**Files:**
- Create: `src/dwf_mcp/instruments/i2c.py`
- Create: `tests/unit/test_i2c.py`
- Modify: `src/dwf_mcp/server.py` (register I2C)
- Modify: `tests/integration/test_server.py`

**Background:** `configure(sda_pin, scl_pin, clock_hz, pullups?)` claims the two DIO pins, configures protocol params. `write/read/write_read` are passthroughs. `scan()` walks 0x08–0x77 calling `write_one` and returns the list of ACK'd addresses. Pin allocation is by name — `dio0`–`dio15`.

**Step 1: Write failing tests**

Create `tests/unit/test_i2c.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from dwf_mcp.allocator import PinAllocationError, PinAllocator
from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
from dwf_mcp.instrument import InstrumentNotConfigured
from dwf_mcp.instruments.i2c import I2C
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def device(tmp_path: Path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


@pytest.fixture
def i2c(device: DwfDevice, tmp_path: Path) -> I2C:
    device.open()
    return I2C(device=device, artifacts=ArtifactWriter(workspace=tmp_path))


def test_configure_claims_dio_pins(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    assert i2c.device.allocator.claimed_pins() == {"dio0": "i2c", "dio1": "i2c"}


def test_configure_rejects_conflicting_pins(i2c: I2C) -> None:
    i2c.device.allocator.claim("uart", ["dio0"])
    with pytest.raises(PinAllocationError):
        i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)


def test_reconfigure_swaps_pins(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    i2c.configure(sda_pin="dio4", scl_pin="dio5", clock_hz=400_000)
    assert i2c.device.allocator.claimed_pins() == {"dio4": "i2c", "dio5": "i2c"}


def test_write_without_configure_raises(i2c: I2C) -> None:
    with pytest.raises(InstrumentNotConfigured):
        i2c.write(address=0x50, data=b"\x00")


def test_write_returns_ack_status(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_acks({0x50: True})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.write(address=0x50, data=b"\x00\x01")
    assert result == {"address": 0x50, "ack": True, "nak_count": 0}


def test_read_returns_bytes_hex(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_reads({0x50: b"\xde\xad"})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.read(address=0x50, length=2)
    assert result == {"address": 0x50, "data_hex": "dead", "data": [0xde, 0xad]}


def test_write_read_combined(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_reads({0x50: b"\x01\x02\x03"})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.write_read(address=0x50, write=[0x10], read_length=3)
    assert result["data_hex"] == "010203"


def test_scan_returns_acked_addresses(i2c: I2C) -> None:
    i2c.device.backend.set_i2c_acks({0x20: True, 0x50: True})  # type: ignore[attr-defined]
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    result = i2c.scan()
    assert result["found"] == [0x20, 0x50]
    assert result["count"] == 2


def test_release_clears_pins(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
    i2c.release()
    assert i2c.device.allocator.claimed_pins() == {}


def test_pullups_kept_for_sidecar(i2c: I2C) -> None:
    i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000, pullups=True)
    assert i2c._pullups is True  # type: ignore[attr-defined]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_i2c.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement I2C**

Create `src/dwf_mcp/instruments/i2c.py`:

```python
"""I2C active-master instrument. Wraps pydwf.ProtocolI2C via the DwfBackend seam."""
from __future__ import annotations

import re
from typing import Any, ClassVar

from dwf_mcp.artifacts import ArtifactWriter
from dwf_mcp.device import DwfDevice
from dwf_mcp.instrument import Instrument, InstrumentNotConfigured

_DIO_PATTERN = re.compile(r"^dio(\d+)$")

I2C_CONFIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["sda_pin", "scl_pin", "clock_hz"],
    "properties": {
        "sda_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "scl_pin": {"type": "string", "pattern": "^dio[0-9]+$"},
        "clock_hz": {"type": "number", "minimum": 100, "maximum": 1_000_000},
        "pullups": {"type": "boolean", "default": False},
        "timeout_s": {"type": "number", "minimum": 0.0, "default": 0.1},
        "stretch": {"type": "boolean", "default": True},
    },
}

I2C_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["address", "data"],
    "properties": {
        "address": {"type": "integer", "minimum": 0, "maximum": 0x7F},
        "data": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 0xFF}},
    },
}

I2C_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["address", "length"],
    "properties": {
        "address": {"type": "integer", "minimum": 0, "maximum": 0x7F},
        "length": {"type": "integer", "minimum": 1, "maximum": 4096},
    },
}

I2C_WRITE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["address", "write", "read_length"],
    "properties": {
        "address": {"type": "integer", "minimum": 0, "maximum": 0x7F},
        "write": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 0xFF}},
        "read_length": {"type": "integer", "minimum": 0, "maximum": 4096},
    },
}

I2C_SCAN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _dio_index(pin: str) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected pin like 'dio0'..'dio15', got {pin!r}")
    return int(m.group(1))


def _to_bytes(data: list[int] | bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    return bytes(data)


class I2C(Instrument):
    name = "i2c"
    tools: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {
        "configure":  ("configure",  I2C_CONFIGURE_SCHEMA),
        "write":      ("write",      I2C_WRITE_SCHEMA),
        "read":       ("read",       I2C_READ_SCHEMA),
        "write_read": ("write_read", I2C_WRITE_READ_SCHEMA),
        "scan":       ("scan",       I2C_SCAN_SCHEMA),
    }

    def __init__(self, device: DwfDevice, artifacts: ArtifactWriter) -> None:
        self.device = device
        self.artifacts = artifacts
        self._configured = False
        self._sda_pin: str | None = None
        self._scl_pin: str | None = None
        self._clock_hz: float = 0
        self._pullups: bool = False

    def configure(self, sda_pin: str, scl_pin: str, clock_hz: float,
                  pullups: bool = False, timeout_s: float = 0.1, stretch: bool = True) -> dict[str, Any]:
        if sda_pin == scl_pin:
            raise ValueError("sda_pin and scl_pin must be different")
        sda_idx = _dio_index(sda_pin)
        scl_idx = _dio_index(scl_pin)
        self.device.allocator.claim("i2c", [sda_pin, scl_pin])
        self.device.backend.i2c_reset()
        self.device.backend.i2c_configure(
            scl_pin_idx=scl_idx, sda_pin_idx=sda_idx,
            rate_hz=clock_hz, stretch=stretch, timeout_s=timeout_s,
        )
        self._configured = True
        self._sda_pin = sda_pin
        self._scl_pin = scl_pin
        self._clock_hz = clock_hz
        self._pullups = pullups
        return {"configured": True, "sda_pin": sda_pin, "scl_pin": scl_pin,
                "clock_hz": clock_hz, "pullups": pullups}

    def write(self, address: int, data: list[int] | bytes) -> dict[str, Any]:
        self._require_configured()
        nak = self.device.backend.i2c_write(address=address, data=_to_bytes(data))
        return {"address": address, "ack": nak == 0, "nak_count": nak}

    def read(self, address: int, length: int) -> dict[str, Any]:
        self._require_configured()
        data = self.device.backend.i2c_read(address=address, length=length)
        return {"address": address, "data_hex": data.hex(), "data": list(data)}

    def write_read(self, address: int, write: list[int] | bytes, read_length: int) -> dict[str, Any]:
        self._require_configured()
        data = self.device.backend.i2c_write_read(
            address=address, write_data=_to_bytes(write), read_length=read_length,
        )
        return {"address": address, "data_hex": data.hex(), "data": list(data)}

    def scan(self) -> dict[str, Any]:
        self._require_configured()
        found: list[int] = []
        for addr in range(0x08, 0x78):
            nak = self.device.backend.i2c_write_one(address=addr, byte=0)
            if nak == 0:
                found.append(addr)
        return {"found": found, "count": len(found)}

    def release(self) -> None:
        self.device.allocator.release("i2c")
        self._configured = False
        self._sda_pin = None
        self._scl_pin = None

    def _require_configured(self) -> None:
        if not self._configured:
            raise InstrumentNotConfigured("i2c.configure must be called before any I/O operation")
```

**Step 4: Register I2C in `build_app`**

```python
from dwf_mcp.instruments.i2c import I2C
```
And `app.register_instrument(I2C)`.

**Step 5: Append integration test**

```python
@pytest.mark.asyncio
async def test_i2c_configure_scan_flow(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    app.device.backend.set_i2c_acks({0x50: True, 0x68: True})  # type: ignore[attr-defined]
    await app.call_tool("waveforms.open", {})
    await app.call_tool("i2c.configure", {
        "sda_pin": "dio0", "scl_pin": "dio1", "clock_hz": 100_000,
    })
    scan = await app.call_tool("i2c.scan", {})
    assert scan["found"] == [0x50, 0x68]
    await app.call_tool("waveforms.close", {})


@pytest.mark.asyncio
async def test_i2c_write_before_configure_returns_error(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    result = await app.call_tool("i2c.write", {"address": 0x50, "data": [0x00]})
    assert result["error"]["type"] == "InstrumentNotConfigured"
```

**Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_i2c.py tests/integration/test_server.py -v`

Run: `pytest -m "not hardware"` — expect 79 + 10 + 2 = 91 passed.
Run: `ruff check . && mypy src/dwf_mcp` — clean.

**Step 7: Commit**

```bash
git add src/dwf_mcp/instruments/i2c.py src/dwf_mcp/server.py tests/unit/test_i2c.py tests/integration/test_server.py
git commit -m "$(cat <<'EOF'
feat(i2c): active master with configure/write/read/write_read/scan

I2C instrument routes through pydwf.ProtocolI2C via the DwfBackend
seam. configure claims sda/scl DIO pins atomically and stashes pullup
metadata for sidecars. scan walks 0x08-0x77 with write_one and returns
the ACK'd address list. write/read/write_read return data_hex + int
list for easy Claude-side handling.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: PydwfBackend ProtocolI2C + i2c hardware smoke

**Files:**
- Modify: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/hardware/test_i2c_hardware.py`

**Background:** Map i2c methods to `pydwf.ProtocolI2C`. Method names confirmed via introspection: `sclSet`, `sdaSet`, `rateSet`, `stretchSet`, `timeoutSet`, `reset`, `write`, `read`, `writeRead`, `writeOne`. Returns from `write`/`writeOne` are NAK counts; `read`/`writeRead` return bytes-like.

**Step 1: Extend PydwfBackend**

In `src/dwf_mcp/backends/pydwf_backend.py`, add:

```python
    @property
    def _i2c(self) -> Any:
        if self._device is None:
            raise DwfBackendError("device not open")
        return self._device.protocol.i2c

    def i2c_configure(self, scl_pin_idx: int, sda_pin_idx: int, rate_hz: float,
                      stretch: bool, timeout_s: float) -> None:
        i2c = self._i2c
        i2c.sclSet(scl_pin_idx)
        i2c.sdaSet(sda_pin_idx)
        i2c.rateSet(rate_hz)
        i2c.stretchSet(1 if stretch else 0)
        i2c.timeoutSet(timeout_s)

    def i2c_reset(self) -> None:
        self._i2c.reset()

    def i2c_write(self, address: int, data: bytes) -> int:
        return int(self._i2c.write(address << 1, data))

    def i2c_read(self, address: int, length: int) -> bytes:
        result = self._i2c.read(address << 1, length)
        return bytes(result)

    def i2c_write_read(self, address: int, write_data: bytes, read_length: int) -> bytes:
        result = self._i2c.writeRead(address << 1, write_data, read_length)
        return bytes(result)

    def i2c_write_one(self, address: int, byte: int) -> int:
        return int(self._i2c.writeOne(address << 1, byte))
```

**Note 1:** pydwf's I2C protocol uses 8-bit addressing (7-bit address shifted left). The `<< 1` reflects that. Verify behavior against pydwf docs — if pydwf already shifts internally, remove the `<< 1`.

**Note 2:** the protocol accessor — `self._device.protocol.i2c` — is one possibility; pydwf may instead expose `self._device.protocolI2C` directly. Verify with:
```bash
. .venv/bin/activate && python -c "from pydwf import DwfLibrary; print([m for m in dir(DwfLibrary().deviceControl) if not m.startswith('_')])"
```
and inspect the open-device handle for protocol access.

**Step 2: Write hardware smoke test**

Create `tests/hardware/test_i2c_hardware.py`:

```python
"""Hardware smoke for i2c. Requires AD3 with bench pull-ups on the configured DIO pins.

Even without any I2C slave on the bus, the scan should run cleanly and return [] —
proving the wire toggled and the protocol class came up.
"""
from __future__ import annotations

import pytest


@pytest.mark.hardware
def test_i2c_scan_runs_without_error(tmp_path) -> None:
    pytest.importorskip("pydwf")
    from dwf_mcp.allocator import PinAllocator
    from dwf_mcp.artifacts import ArtifactWriter
    from dwf_mcp.backends.pydwf_backend import PydwfBackend
    from dwf_mcp.device import DwfDevice
    from dwf_mcp.devices.ad3 import AD3_RESOURCE_GROUPS
    from dwf_mcp.instruments.i2c import I2C
    from dwf_mcp.policy import SafetyPolicy

    backend = PydwfBackend()
    device = DwfDevice(
        backend=backend, policy=SafetyPolicy(),
        allocator=PinAllocator(resource_groups=AD3_RESOURCE_GROUPS),
        workspace=tmp_path, idle_timeout_s=60,
    )
    device.open()
    try:
        i2c = I2C(device=device, artifacts=ArtifactWriter(workspace=tmp_path))
        i2c.configure(sda_pin="dio0", scl_pin="dio1", clock_hz=100_000)
        result = i2c.scan()
        assert "found" in result
        assert isinstance(result["found"], list)
        # Empty list is OK (no slaves); the point is the protocol class ran.
    finally:
        device.close()
```

**Step 3: Run unit suite**

Run: `pytest -m "not hardware" -v` — 91 passed. `ruff check . && mypy src/dwf_mcp` — clean.

**Step 4: Run hardware smoke (if AD3 connected with pull-ups)**

Run: `pytest tests/hardware/test_i2c_hardware.py -m hardware -v`

If pydwf protocol class accessor is wrong, fix per the discovery pattern.

**Step 5: Commit**

```bash
git add src/dwf_mcp/backends/pydwf_backend.py tests/hardware/test_i2c_hardware.py
git commit -m "$(cat <<'EOF'
feat(backend): pydwf ProtocolI2C passthroughs + i2c hardware smoke

Wires i2c_configure/reset/write/read/write_read/write_one to
pydwf.ProtocolI2C. Hardware smoke runs scan on dio0/dio1 against bench
pull-ups; empty result is acceptable (no slave required) — the test
proves the protocol class came up and the wire toggled.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: End-of-stage verification + README update

**Step 1: Full test sweep**

Run: `pytest -m "not hardware" -v`
Expected: ~91 tests, all passing.

Run: `ruff check .`
Expected: clean.

Run: `mypy src/dwf_mcp`
Expected: clean.

**Step 2: Hardware sweep (if AD3 connected with the wiring this plan calls out)**

Run: `pytest -m hardware -v`
Expected: 4 passing (stage 1 backend smoke + stage 2 scope/supply/i2c). If wiring isn't in place, document which tests were not run.

**Step 3: Update README**

Replace the `## Status` block in `README.md` with:

```markdown
## Status

Stage 2 complete (3 of N stages):
- Stage 1: safety policy, pin allocator, artifact writer, instrument ABC + registry, DwfDevice with lazy open/idle/unplug recovery, DwfBackend ABC + fake + pydwf, MCP server with `waveforms.open/close/status/list_pins`.
- Stage 2: `scope` (buffer-mode capture), `supply` (safety-gated programmable rails), `i2c` (active master). Centralized `device.gate_output` safety helper; `dwf-safety.log` audit trail; lazy-instantiated instruments with `tools`-map dispatch and exception → result-shape error mapping.

Stage 3: `awg`, `logic`, `pattern`, `dio`, `dmm`, `can`, `spi`, `uart`. Streaming/recording modes. VCD writer. AD3 pin-map verification against the reference manual (load-bearing now that AWG / logic / pattern start to overlap on DIO/clock domains).
Stage 4: passive decoders.
```

Commit:
```bash
git add README.md
git commit -m "docs: stage 2 status in README"
```

**Step 4: Final sanity check**

Run: `git status` — clean.
Run: `git log --oneline main..HEAD` — should show 13 stage 2 commits + the design doc.

**Step 5: Hand off**

Per the handoff doc's token-strategy note, check in with the user — they'll decide whether to merge to main or PR-review first. Don't push or merge autonomously.

---

## Out of scope (saved for stage 3)

- `awg`, `logic`, `pattern`, `dio`, `dmm`, `can`, `spi`, `uart` instruments
- `scope.record` streaming mode
- VCD writer (lands with `logic.capture`)
- Trigger I/O pin support (`trig1`/`trig2`)
- System monitor (`system.monitor`)
- Per-device firmware probing
- AD3 pin-map verification against the reference manual (becomes load-bearing in stage 3)

## Open questions for execution

These shouldn't block stage 2, but flag in commit messages if encountered:

1. **pydwf method names.** Any discrepancy between the names in this plan and the actual pydwf API found in the venv — use the discovery pattern (`python -c "from pydwf import X; print(dir(X))"`), update the call, note in the commit. Stage 1's task 9 caught one such case (`serialNumber` vs `deviceSerialNumber`).
2. **I2C address shift.** Pydwf's ProtocolI2C `address` argument may or may not auto-shift the 7-bit address. The plan does `address << 1`; if scans return wrong addresses, drop the shift.
3. **AnalogIO node names on AD3.** Supply discovery matches case-insensitive substrings (`v+`/`positive`/`vpos` for vpos rail). If discovery fails on real hardware, run:
   ```bash
   python -c "
   from pydwf import DwfLibrary
   d = DwfLibrary().deviceControl.open(0)
   aio = d.analogIO
   for i in range(aio.channelCount()):
       print(i, aio.channelName(i))
       for j in range(aio.channelInfo(i)):
           print(' ', j, aio.channelNodeName(i, j))
   d.close()
   "
   ```
   and adjust the substring matches in `pydwf_backend.py:supply_discover_nodes`.
