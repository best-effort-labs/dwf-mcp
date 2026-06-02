# dwf-mcp Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Lay down the non-instrument scaffolding for the dwf-mcp server — project structure, safety policy, pin allocator, artifact writer, instrument ABC + registry, device layer with both a fake (hardware-free) backend and a real `pydwf` backend, and the MCP server entry with meta tools (`open`/`close`/`status`/`list_pins`). No actual instruments are wired up in this plan; that's stage 2.

**Architecture:** Python package using the `mcp` SDK (stdio) and `pydwf` (Digilent's maintained `libdwf` wrapper). Layered design: a `DwfBackend` ABC sits below `DwfDevice` so unit tests can run against a `FakeBackend` and real work runs against `PydwfBackend`. Above the device sit the `Instrument` ABC, the registry, the safety policy, the pin allocator. The MCP server is thin glue. See `docs/plans/2026-06-02-dwf-mcp-design.md` for the full design.

**Tech Stack:** Python 3.11+, `mcp`, `pydwf`, `numpy`, `pyarrow`, `pytest`, `pytest-asyncio`, `hatchling` build backend, `src/` layout.

---

## Conventions

- **TDD always.** Test first, then minimal implementation.
- **Commit each task.** Conventional Commit style (`feat:`, `chore:`, `test:`, `refactor:`). Co-author trailer optional but consistent.
- **No hardware in unit tests.** Anything touching `pydwf` for real goes under `tests/hardware/` with `@pytest.mark.hardware` and is skipped by default.
- **Type hints everywhere.** `from __future__ import annotations` at the top of every module.
- **No `print`.** Use `logging.getLogger(__name__)`.
- **Run all tests after each task.** `pytest -m "not hardware"` from repo root must stay green.

## Reference

- Design doc: `docs/plans/2026-06-02-dwf-mcp-design.md`
- `pydwf` docs: https://pydwf.readthedocs.io/
- `mcp` Python SDK: https://github.com/modelcontextprotocol/python-sdk
- AD3 reference manual: Digilent docs; consult for the pin/resource-group table when building stage 2

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/dwf_mcp/__init__.py`
- Create: `src/dwf_mcp/py.typed`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/hardware/__init__.py`
- Create: `tests/integration/__init__.py`
- Create: `conftest.py`
- Create: `README.md`

**Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "dwf-mcp"
version = "0.1.0"
description = "MCP server exposing the Digilent WaveForms SDK"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.0",
    "pydwf>=1.1",
    "numpy>=1.26",
    "pyarrow>=15",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "ruff>=0.4",
    "mypy>=1.10",
]

[project.scripts]
dwf-mcp = "dwf_mcp.server:main"

[tool.hatch.build.targets.wheel]
packages = ["src/dwf_mcp"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "hardware: requires a physically connected AD3 (deselect with -m 'not hardware')",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
strict = true
```

**Step 2: Create package skeleton**

`src/dwf_mcp/__init__.py`:
```python
"""dwf-mcp: MCP server for the Digilent WaveForms SDK."""
from __future__ import annotations

__version__ = "0.1.0"
```

`src/dwf_mcp/py.typed`: empty file (marks the package as typed for downstream consumers).

`tests/__init__.py`, `tests/unit/__init__.py`, `tests/hardware/__init__.py`, `tests/integration/__init__.py`: all empty.

`conftest.py`:
```python
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m") and "hardware" in config.getoption("-m"):
        return
    skip_hw = pytest.mark.skip(reason="hardware tests require -m hardware")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip_hw)
```

`README.md`:
```markdown
# dwf-mcp

MCP server exposing the Digilent WaveForms SDK.

See `docs/plans/2026-06-02-dwf-mcp-design.md` for design.
```

**Step 3: Install dev environment**

Run:
```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Expected: clean install. If `pydwf` install fails on macOS, the WaveForms runtime must be installed first (download from Digilent). Note in README if it bites.

**Step 4: Verify baseline**

Run: `pytest -m "not hardware" -v`
Expected: 0 tests collected, exit 0 (or 5 if pytest treats no-tests as failure — both OK at this stage).

Run: `ruff check .`
Expected: no issues.

**Step 5: Commit**

```bash
git add pyproject.toml src/ tests/ conftest.py README.md
git commit -m "chore: project scaffolding (hatchling, pytest, ruff, mypy)"
```

---

### Task 2: SafetyPolicy

**Files:**
- Create: `src/dwf_mcp/policy.py`
- Create: `tests/unit/test_policy.py`

**Step 1: Write failing tests**

`tests/unit/test_policy.py`:
```python
from __future__ import annotations

import pytest

from dwf_mcp.policy import SafetyPolicy, SafetyViolation


def test_default_policy_blocks_outputs() -> None:
    p = SafetyPolicy()
    assert p.require_explicit_enable is True


def test_supply_voltage_cap_enforced() -> None:
    p = SafetyPolicy(supply_max_voltage_pos=3.3)
    p.check_supply_voltage("pos", 3.3)  # boundary OK
    with pytest.raises(SafetyViolation) as exc:
        p.check_supply_voltage("pos", 3.31)
    assert "3.31" in str(exc.value)
    assert "3.3" in str(exc.value)


def test_supply_negative_cap_enforced() -> None:
    p = SafetyPolicy(supply_max_voltage_neg=-3.3)
    p.check_supply_voltage("neg", -3.3)
    with pytest.raises(SafetyViolation):
        p.check_supply_voltage("neg", -3.31)


def test_supply_current_cap_enforced() -> None:
    p = SafetyPolicy(supply_max_current=0.5)
    p.check_supply_current(0.5)
    with pytest.raises(SafetyViolation):
        p.check_supply_current(0.51)


def test_awg_amplitude_cap_enforced() -> None:
    p = SafetyPolicy(awg_max_amplitude=3.3)
    p.check_awg_amplitude(3.3)
    with pytest.raises(SafetyViolation):
        p.check_awg_amplitude(3.31)


def test_pattern_voltage_setting_exposed() -> None:
    p = SafetyPolicy(pattern_voltage="1.8")
    assert p.pattern_voltage == "1.8"


def test_policy_is_frozen() -> None:
    p = SafetyPolicy(supply_max_voltage_pos=3.3)
    with pytest.raises((AttributeError, TypeError)):
        p.supply_max_voltage_pos = 5.0  # type: ignore[misc]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_policy.py -v`
Expected: `ModuleNotFoundError: No module named 'dwf_mcp.policy'`.

**Step 3: Implement `SafetyPolicy`**

`src/dwf_mcp/policy.py`:
```python
from __future__ import annotations

from dataclasses import dataclass


class SafetyViolation(Exception):
    """Raised when a tool call would exceed the active SafetyPolicy."""


@dataclass(frozen=True)
class SafetyPolicy:
    supply_max_voltage_pos: float = 3.3
    supply_max_voltage_neg: float = -3.3
    supply_max_current: float = 0.5
    awg_max_amplitude: float = 3.3
    pattern_voltage: str = "3.3"
    require_explicit_enable: bool = True

    def check_supply_voltage(self, channel: str, voltage: float) -> None:
        if channel == "pos" and voltage > self.supply_max_voltage_pos:
            raise SafetyViolation(
                f"supply.pos voltage {voltage} V exceeds policy cap "
                f"{self.supply_max_voltage_pos} V"
            )
        if channel == "neg" and voltage < self.supply_max_voltage_neg:
            raise SafetyViolation(
                f"supply.neg voltage {voltage} V exceeds policy cap "
                f"{self.supply_max_voltage_neg} V"
            )

    def check_supply_current(self, current: float) -> None:
        if current > self.supply_max_current:
            raise SafetyViolation(
                f"supply current {current} A exceeds policy cap "
                f"{self.supply_max_current} A"
            )

    def check_awg_amplitude(self, amplitude: float) -> None:
        if amplitude > self.awg_max_amplitude:
            raise SafetyViolation(
                f"AWG amplitude {amplitude} V exceeds policy cap "
                f"{self.awg_max_amplitude} V"
            )
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_policy.py -v`
Expected: all 7 tests pass.

**Step 5: Commit**

```bash
git add src/dwf_mcp/policy.py tests/unit/test_policy.py
git commit -m "feat(policy): SafetyPolicy with voltage/current/amplitude caps"
```

---

### Task 3: PinAllocator + AD3 resource groups

**Files:**
- Create: `src/dwf_mcp/allocator.py`
- Create: `src/dwf_mcp/devices/__init__.py`
- Create: `src/dwf_mcp/devices/ad3.py`
- Create: `tests/unit/test_allocator.py`

**Background:** Pin-level arbitration — instruments declare which DIO/analog channels they claim at configure time. Overlaps rejected before any DWF call. AD3 hard constraints (e.g. AWG ch1 and ch2 may share a clock domain; the two scope channels are co-sampled) are encoded as resource groups: claiming any pin in a group locks the rest from incompatible uses.

**Step 1: Write failing tests**

`tests/unit/test_allocator.py`:
```python
from __future__ import annotations

import pytest

from dwf_mcp.allocator import PinAllocator, PinAllocationError, ResourceGroup


@pytest.fixture
def alloc() -> PinAllocator:
    return PinAllocator(resource_groups=[])


def test_claim_then_release_frees_pins(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    with pytest.raises(PinAllocationError):
        alloc.claim("uart", ["dio0", "dio2"])
    alloc.release("i2c")
    alloc.claim("uart", ["dio0", "dio2"])  # now OK


def test_claim_lists_claimed_pins(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    alloc.claim("uart", ["dio2", "dio3"])
    assert alloc.claimed_pins() == {
        "dio0": "i2c", "dio1": "i2c", "dio2": "uart", "dio3": "uart"
    }


def test_double_claim_by_same_instrument_is_replacement(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    alloc.claim("i2c", ["dio4", "dio5"])  # reconfigure
    assert alloc.claimed_pins() == {"dio4": "i2c", "dio5": "i2c"}


def test_resource_group_conflict() -> None:
    # Scope ch1 and ch2 are co-sampled: configuring one locks the other for the same instrument.
    groups = [ResourceGroup(name="scope_pair", pins={"scope1", "scope2"}, exclusive=True)]
    alloc = PinAllocator(resource_groups=groups)
    alloc.claim("scope", ["scope1"])
    # A different instrument cannot grab scope2 either, because the group is exclusive.
    with pytest.raises(PinAllocationError) as exc:
        alloc.claim("other", ["scope2"])
    assert "scope_pair" in str(exc.value)


def test_release_unknown_instrument_is_noop(alloc: PinAllocator) -> None:
    alloc.release("never_claimed")  # no raise


def test_clear_releases_everything(alloc: PinAllocator) -> None:
    alloc.claim("i2c", ["dio0", "dio1"])
    alloc.claim("uart", ["dio2", "dio3"])
    alloc.clear()
    assert alloc.claimed_pins() == {}
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_allocator.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement allocator**

`src/dwf_mcp/allocator.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field


class PinAllocationError(Exception):
    """Raised when an instrument tries to claim pins already in use, or a resource group conflict."""


@dataclass(frozen=True)
class ResourceGroup:
    name: str
    pins: frozenset[str]
    exclusive: bool = True  # any claim on any pin locks the rest

    def __init__(self, name: str, pins: set[str] | frozenset[str], exclusive: bool = True) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "pins", frozenset(pins))
        object.__setattr__(self, "exclusive", exclusive)


@dataclass
class PinAllocator:
    resource_groups: list[ResourceGroup] = field(default_factory=list)
    _claims: dict[str, list[str]] = field(default_factory=dict)  # instrument -> pins

    def claim(self, instrument: str, pins: list[str]) -> None:
        # Replacement semantics: re-claiming for the same instrument frees its old pins first.
        self._claims.pop(instrument, None)
        pin_owners = self.claimed_pins()
        for pin in pins:
            if pin in pin_owners:
                raise PinAllocationError(
                    f"{instrument} cannot claim {pin}: already held by {pin_owners[pin]}"
                )
        for group in self.resource_groups:
            if not group.exclusive:
                continue
            requested_in_group = group.pins & set(pins)
            if not requested_in_group:
                continue
            for other_instr, other_pins in self._claims.items():
                if other_instr == instrument:
                    continue
                if set(other_pins) & group.pins:
                    raise PinAllocationError(
                        f"{instrument} cannot claim {sorted(requested_in_group)}: "
                        f"resource group {group.name!r} is held by {other_instr}"
                    )
        self._claims[instrument] = list(pins)

    def release(self, instrument: str) -> None:
        self._claims.pop(instrument, None)

    def claimed_pins(self) -> dict[str, str]:
        return {pin: instr for instr, pins in self._claims.items() for pin in pins}

    def claimed_instruments(self) -> list[str]:
        return list(self._claims.keys())

    def clear(self) -> None:
        self._claims.clear()
```

`src/dwf_mcp/devices/__init__.py`: empty.

`src/dwf_mcp/devices/ad3.py`:
```python
"""AD3 pin map and resource groups. Refine against the AD3 reference manual before stage 2."""
from __future__ import annotations

from dwf_mcp.allocator import ResourceGroup


# Provisional. Confirm against AD3 reference manual when wiring real instruments in stage 2.
AD3_DIO_PINS: list[str] = [f"dio{i}" for i in range(16)]
AD3_ANALOG_IN_PINS: list[str] = ["scope1", "scope2"]
AD3_ANALOG_OUT_PINS: list[str] = ["awg1", "awg2"]
AD3_SUPPLY_PINS: list[str] = ["vpos", "vneg"]
AD3_TRIGGER_PINS: list[str] = ["trig1", "trig2"]

AD3_RESOURCE_GROUPS: list[ResourceGroup] = [
    # Scope channels are co-sampled — claiming one for the scope locks the pair.
    # Marked non-exclusive here so the *same* instrument can claim both; cross-instrument
    # conflict is still caught because pin ownership is per-pin.
    ResourceGroup(name="scope_pair", pins=set(AD3_ANALOG_IN_PINS), exclusive=False),
    # AWG channels share a clock domain. Two different instruments can't both drive AWG outputs.
    ResourceGroup(name="awg_clock", pins=set(AD3_ANALOG_OUT_PINS), exclusive=True),
]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_allocator.py -v`
Expected: all 6 tests pass.

**Step 5: Commit**

```bash
git add src/dwf_mcp/allocator.py src/dwf_mcp/devices/ tests/unit/test_allocator.py
git commit -m "feat(allocator): pin allocator with resource groups; AD3 provisional pin map"
```

---

### Task 4: ArtifactWriter

**Files:**
- Create: `src/dwf_mcp/artifacts.py`
- Create: `tests/unit/test_artifacts.py`

**Step 1: Write failing tests**

`tests/unit/test_artifacts.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dwf_mcp.artifacts import ArtifactWriter, CaptureSummary


@pytest.fixture
def writer(tmp_path: Path) -> ArtifactWriter:
    return ArtifactWriter(workspace=tmp_path)


def test_writer_creates_captures_dir(writer: ArtifactWriter, tmp_path: Path) -> None:
    assert (tmp_path / "captures").is_dir()


def test_write_npz_returns_path_and_sidecar(writer: ArtifactWriter, tmp_path: Path) -> None:
    samples = np.arange(100, dtype=np.float32)
    summary = CaptureSummary(
        instrument="scope",
        sample_count=100,
        sample_rate_hz=1_000_000,
        extra={"min": 0.0, "max": 99.0},
    )
    result = writer.write_npz(
        instrument="scope",
        arrays={"ch1": samples},
        config={"channels": [1], "sample_rate_hz": 1_000_000},
        summary=summary,
    )
    assert Path(result.path).is_file()
    assert Path(result.sidecar_path).is_file()
    assert Path(result.path).parent == tmp_path / "captures"

    loaded = np.load(result.path)
    assert np.array_equal(loaded["ch1"], samples)

    sidecar = json.loads(Path(result.sidecar_path).read_text())
    assert sidecar["instrument"] == "scope"
    assert sidecar["config"] == {"channels": [1], "sample_rate_hz": 1_000_000}
    assert sidecar["summary"]["sample_count"] == 100
    assert sidecar["summary"]["extra"]["max"] == 99.0


def test_explicit_output_path_overrides_default(writer: ArtifactWriter, tmp_path: Path) -> None:
    target = tmp_path / "custom.npz"
    result = writer.write_npz(
        instrument="scope",
        arrays={"ch1": np.zeros(10)},
        config={},
        summary=CaptureSummary(instrument="scope", sample_count=10),
        output_path=target,
    )
    assert Path(result.path) == target
    assert Path(result.sidecar_path) == target.with_suffix(".json")


def test_default_workspace_is_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / "fake-temp"))
    (tmp_path / "fake-temp").mkdir()
    writer = ArtifactWriter()
    assert Path(writer.workspace).name == "fake-temp"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_artifacts.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement ArtifactWriter**

`src/dwf_mcp/artifacts.py`:
```python
from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class CaptureSummary:
    instrument: str
    sample_count: int = 0
    sample_rate_hz: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactResult:
    path: str
    sidecar_path: str
    summary: dict[str, Any]


class ArtifactWriter:
    def __init__(self, workspace: Path | str | None = None) -> None:
        if workspace is None:
            workspace = Path(tempfile.mkdtemp(prefix="dwf-"))
        self.workspace = Path(workspace)
        (self.workspace / "captures").mkdir(parents=True, exist_ok=True)

    def write_npz(
        self,
        instrument: str,
        arrays: dict[str, np.ndarray],
        config: dict[str, Any],
        summary: CaptureSummary,
        output_path: Path | None = None,
        description: str | None = None,
    ) -> ArtifactResult:
        if output_path is None:
            output_path = self._default_path(instrument, ".npz")
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        np.savez_compressed(output_path, **arrays)
        sidecar_path = output_path.with_suffix(".json")
        sidecar = {
            "instrument": instrument,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "description": description,
            "config": config,
            "summary": asdict(summary),
        }
        sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str))
        return ArtifactResult(
            path=str(output_path),
            sidecar_path=str(sidecar_path),
            summary=asdict(summary),
        )

    def _default_path(self, instrument: str, suffix: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        name = f"{ts}_{instrument}_{uuid.uuid4().hex[:8]}{suffix}"
        return self.workspace / "captures" / name
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_artifacts.py -v`
Expected: all 4 tests pass.

**Step 5: Commit**

```bash
git add src/dwf_mcp/artifacts.py tests/unit/test_artifacts.py
git commit -m "feat(artifacts): npz writer with JSON sidecar and per-workspace layout"
```

---

### Task 5: Instrument ABC + InstrumentRegistry

**Files:**
- Create: `src/dwf_mcp/instrument.py`
- Create: `src/dwf_mcp/registry.py`
- Create: `tests/unit/test_registry.py`

**Step 1: Write failing tests**

`tests/unit/test_registry.py`:
```python
from __future__ import annotations

import pytest

from dwf_mcp.instrument import Instrument
from dwf_mcp.registry import InstrumentRegistry


class DummyInstrument(Instrument):
    name = "dummy"
    required_pins: list[str] = []

    def configure(self, **kwargs: object) -> None:
        self._configured = True

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
        required_pins: list[str] = []
        def configure(self, **kwargs: object) -> None: ...
        def release(self) -> None: ...
    # name attribute missing should be flagged at registration time
    reg = InstrumentRegistry()
    with pytest.raises(TypeError):
        reg.register(Nameless)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_registry.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement ABC + registry**

`src/dwf_mcp/instrument.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class Instrument(ABC):
    name: ClassVar[str]
    required_pins: ClassVar[list[str]]

    @abstractmethod
    def configure(self, **kwargs: object) -> None: ...

    @abstractmethod
    def release(self) -> None: ...
```

`src/dwf_mcp/registry.py`:
```python
from __future__ import annotations

from dwf_mcp.instrument import Instrument


class InstrumentRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[Instrument]] = {}

    def register(self, cls: type[Instrument]) -> None:
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not name:
            raise TypeError(f"{cls.__name__} must declare a non-empty `name` class attribute")
        if name in self._classes:
            raise ValueError(f"instrument {name!r} already registered")
        self._classes[name] = cls

    def get_class(self, name: str) -> type[Instrument]:
        try:
            return self._classes[name]
        except KeyError as exc:
            raise KeyError(f"unknown instrument {name!r}") from exc

    def names(self) -> list[str]:
        return list(self._classes.keys())
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_registry.py -v`
Expected: all 4 tests pass.

**Step 5: Commit**

```bash
git add src/dwf_mcp/instrument.py src/dwf_mcp/registry.py tests/unit/test_registry.py
git commit -m "feat(registry): Instrument ABC and InstrumentRegistry"
```

---

### Task 6: DwfBackend ABC + FakeBackend

**Files:**
- Create: `src/dwf_mcp/backend.py`
- Create: `src/dwf_mcp/backends/__init__.py`
- Create: `src/dwf_mcp/backends/fake.py`
- Create: `tests/unit/test_fake_backend.py`

**Background:** The `DwfBackend` ABC is the seam between `DwfDevice` and the actual hardware library. It exposes only the operations the device layer needs: enumerate, open, close, basic device info, and a `is_open` flag. Concrete instrument calls (scope configure, supply set, etc.) are added to the backend in later stages as instruments are wired up. `FakeBackend` makes hardware-free unit tests possible.

**Step 1: Write failing tests**

`tests/unit/test_fake_backend.py`:
```python
from __future__ import annotations

import pytest

from dwf_mcp.backend import DeviceInfo, DwfBackendError
from dwf_mcp.backends.fake import FakeBackend


def test_enumerate_finds_fake_device() -> None:
    b = FakeBackend()
    devices = b.enumerate()
    assert len(devices) == 1
    assert devices[0].serial == "FAKE-AD3-0001"
    assert devices[0].model == "Analog Discovery 3"


def test_open_close_lifecycle() -> None:
    b = FakeBackend()
    assert not b.is_open
    info = b.open()
    assert b.is_open
    assert isinstance(info, DeviceInfo)
    b.close()
    assert not b.is_open


def test_double_open_returns_same_info() -> None:
    b = FakeBackend()
    info1 = b.open()
    info2 = b.open()
    assert info1 == info2


def test_open_by_serial_matching() -> None:
    b = FakeBackend()
    b.open(serial="FAKE-AD3-0001")
    b.close()
    with pytest.raises(DwfBackendError):
        b.open(serial="DOES-NOT-EXIST")


def test_simulate_unplug() -> None:
    b = FakeBackend()
    b.open()
    b.simulate_unplug()
    assert not b.is_open
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_fake_backend.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement backend ABC + FakeBackend**

`src/dwf_mcp/backend.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class DwfBackendError(Exception):
    """Raised for backend-level failures (enumeration, open, close)."""


class DwfDeviceLost(DwfBackendError):
    """Raised when the device disappears mid-session (unplug)."""


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    model: str
    firmware: str
    sample_rate_max_hz: float
    dio_count: int
    analog_in_channels: int
    analog_out_channels: int


class DwfBackend(ABC):
    @abstractmethod
    def enumerate(self) -> list[DeviceInfo]: ...

    @abstractmethod
    def open(self, serial: str | None = None) -> DeviceInfo: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...
```

`src/dwf_mcp/backends/__init__.py`: empty.

`src/dwf_mcp/backends/fake.py`:
```python
from __future__ import annotations

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError


_FAKE_DEVICE = DeviceInfo(
    serial="FAKE-AD3-0001",
    model="Analog Discovery 3",
    firmware="fake-1.0",
    sample_rate_max_hz=125_000_000,
    dio_count=16,
    analog_in_channels=2,
    analog_out_channels=2,
)


class FakeBackend(DwfBackend):
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self._devices = devices or [_FAKE_DEVICE]
        self._open_info: DeviceInfo | None = None

    def enumerate(self) -> list[DeviceInfo]:
        return list(self._devices)

    def open(self, serial: str | None = None) -> DeviceInfo:
        if self._open_info is not None:
            return self._open_info
        candidates = [d for d in self._devices if serial is None or d.serial == serial]
        if not candidates:
            raise DwfBackendError(f"no device matches serial {serial!r}")
        self._open_info = candidates[0]
        return self._open_info

    def close(self) -> None:
        self._open_info = None

    @property
    def is_open(self) -> bool:
        return self._open_info is not None

    # Test helpers
    def simulate_unplug(self) -> None:
        self._open_info = None
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_fake_backend.py -v`
Expected: all 5 tests pass.

**Step 5: Commit**

```bash
git add src/dwf_mcp/backend.py src/dwf_mcp/backends/ tests/unit/test_fake_backend.py
git commit -m "feat(backend): DwfBackend ABC and FakeBackend for hardware-free tests"
```

---

### Task 7: DwfDevice (session manager)

**Files:**
- Create: `src/dwf_mcp/device.py`
- Create: `tests/unit/test_device.py`

**Background:** `DwfDevice` owns the session: lazy-open on first request, idle timeout, explicit close, status, hot-unplug recovery, and integration with the allocator + safety policy. The allocator and policy live on the device for tools to reach via dependency injection.

**Step 1: Write failing tests**

`tests/unit/test_device.py`:
```python
from __future__ import annotations

import asyncio
import time

import pytest

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DwfDeviceLost
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.policy import SafetyPolicy


@pytest.fixture
def device(tmp_path) -> DwfDevice:
    return DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=60,
    )


def test_open_returns_device_info(device: DwfDevice) -> None:
    info = device.open()
    assert info.model == "Analog Discovery 3"
    assert device.is_open


def test_open_is_idempotent(device: DwfDevice) -> None:
    info1 = device.open()
    info2 = device.open()
    assert info1 == info2


def test_close_releases_handle_and_pins(device: DwfDevice) -> None:
    device.open()
    device.allocator.claim("i2c", ["dio0", "dio1"])
    device.close()
    assert not device.is_open
    assert device.allocator.claimed_pins() == {}


def test_status_reports_open_state(device: DwfDevice) -> None:
    status = device.status()
    assert status["open"] is False

    device.open()
    device.allocator.claim("i2c", ["dio0", "dio1"])
    status = device.status()
    assert status["open"] is True
    assert status["device"]["serial"] == "FAKE-AD3-0001"
    assert status["claimed_pins"] == {"dio0": "i2c", "dio1": "i2c"}
    assert status["claimed_instruments"] == ["i2c"]


def test_hot_unplug_marks_session_dead(device: DwfDevice) -> None:
    device.open()
    device.backend.simulate_unplug()  # type: ignore[attr-defined]
    # require_open should now raise
    with pytest.raises(DwfDeviceLost):
        device.require_open()


def test_require_open_returns_info_when_alive(device: DwfDevice) -> None:
    device.open()
    info = device.require_open()
    assert info.model == "Analog Discovery 3"


def test_idle_close_after_timeout(tmp_path) -> None:
    backend = FakeBackend()
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=0.05,
    )
    device.open()
    assert device.is_open
    time.sleep(0.15)
    device.tick_idle()  # caller invokes between tool calls
    assert not device.is_open


def test_activity_resets_idle_timer(tmp_path) -> None:
    device = DwfDevice(
        backend=FakeBackend(),
        policy=SafetyPolicy(),
        allocator=PinAllocator(),
        workspace=tmp_path,
        idle_timeout_s=0.2,
    )
    device.open()
    time.sleep(0.1)
    device.mark_activity()
    time.sleep(0.15)
    device.tick_idle()
    assert device.is_open
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_device.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement `DwfDevice`**

`src/dwf_mcp/device.py`:
```python
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfDeviceLost
from dwf_mcp.policy import SafetyPolicy


class DwfDevice:
    def __init__(
        self,
        backend: DwfBackend,
        policy: SafetyPolicy,
        allocator: PinAllocator,
        workspace: Path | str,
        idle_timeout_s: float = 600.0,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.allocator = allocator
        self.workspace = Path(workspace)
        self.idle_timeout_s = idle_timeout_s
        self._info: DeviceInfo | None = None
        self._last_activity: float | None = None
        self._serial_request: str | None = None

    @property
    def is_open(self) -> bool:
        if self._info is None:
            return False
        # If backend dropped out from under us (unplug), reflect that.
        if not self.backend.is_open:
            self._info = None
            self.allocator.clear()
            return False
        return True

    def open(self, serial: str | None = None) -> DeviceInfo:
        if self.is_open:
            return self._info  # type: ignore[return-value]
        info = self.backend.open(serial=serial)
        self._info = info
        self._serial_request = serial
        self.mark_activity()
        return info

    def close(self) -> None:
        self.allocator.clear()
        if self.backend.is_open:
            self.backend.close()
        self._info = None
        self._last_activity = None

    def require_open(self) -> DeviceInfo:
        if not self.is_open:
            raise DwfDeviceLost("device session is not open (closed, unplugged, or idle-expired)")
        self.mark_activity()
        return self._info  # type: ignore[return-value]

    def mark_activity(self) -> None:
        self._last_activity = time.monotonic()

    def tick_idle(self) -> None:
        if self._info is None or self._last_activity is None:
            return
        if time.monotonic() - self._last_activity >= self.idle_timeout_s:
            self.close()

    def status(self) -> dict[str, Any]:
        idle_remaining: float | None = None
        if self._last_activity is not None:
            elapsed = time.monotonic() - self._last_activity
            idle_remaining = max(0.0, self.idle_timeout_s - elapsed)
        info = None
        if self._info is not None:
            info = {
                "serial": self._info.serial,
                "model": self._info.model,
                "firmware": self._info.firmware,
            }
        return {
            "open": self.is_open,
            "device": info,
            "workspace": str(self.workspace),
            "claimed_pins": self.allocator.claimed_pins(),
            "claimed_instruments": self.allocator.claimed_instruments(),
            "idle_remaining_s": idle_remaining,
            "policy": {
                "supply_max_voltage_pos": self.policy.supply_max_voltage_pos,
                "supply_max_voltage_neg": self.policy.supply_max_voltage_neg,
                "supply_max_current": self.policy.supply_max_current,
                "awg_max_amplitude": self.policy.awg_max_amplitude,
                "pattern_voltage": self.policy.pattern_voltage,
                "require_explicit_enable": self.policy.require_explicit_enable,
            },
        }
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_device.py -v`
Expected: all 8 tests pass.

**Step 5: Commit**

```bash
git add src/dwf_mcp/device.py tests/unit/test_device.py
git commit -m "feat(device): DwfDevice with lazy open, idle timeout, unplug recovery"
```

---

### Task 8: MCP server entry + meta tools

**Files:**
- Create: `src/dwf_mcp/server.py`
- Create: `tests/integration/test_server.py`

**Background:** Use the `mcp` Python SDK's `Server` API. Expose `waveforms.open`, `waveforms.close`, `waveforms.status`, `waveforms.list_pins`. The server holds a single `DwfDevice` instance; backend choice is via env var (`DWF_BACKEND=fake|pydwf`, default `pydwf`). Integration test runs the server in-process against the fake backend.

**Step 1: Write failing tests**

`tests/integration/test_server.py`:
```python
from __future__ import annotations

import pytest

from dwf_mcp.server import build_app


@pytest.mark.asyncio
async def test_open_then_status_then_close(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))

    open_result = await app.call_tool("waveforms.open", {})
    assert open_result["device"]["serial"] == "FAKE-AD3-0001"

    status = await app.call_tool("waveforms.status", {})
    assert status["open"] is True

    close_result = await app.call_tool("waveforms.close", {})
    assert close_result["closed"] is True

    status = await app.call_tool("waveforms.status", {})
    assert status["open"] is False


@pytest.mark.asyncio
async def test_list_pins_reflects_claims(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {})
    app.device.allocator.claim("test", ["dio0", "dio1"])
    pins = await app.call_tool("waveforms.list_pins", {})
    assert pins["claimed"] == {"dio0": "test", "dio1": "test"}
    assert "dio0" in pins["all_pins"]


@pytest.mark.asyncio
async def test_open_accepts_safety_policy_kwargs(tmp_path) -> None:
    app = build_app(backend_name="fake", workspace=str(tmp_path))
    await app.call_tool("waveforms.open", {"supply_max_voltage_pos": 1.8})
    status = await app.call_tool("waveforms.status", {})
    assert status["policy"]["supply_max_voltage_pos"] == 1.8
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_server.py -v`
Expected: `ModuleNotFoundError`.

**Step 3: Implement server**

`src/dwf_mcp/server.py`:
```python
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from dwf_mcp.allocator import PinAllocator
from dwf_mcp.backend import DwfBackend
from dwf_mcp.backends.fake import FakeBackend
from dwf_mcp.device import DwfDevice
from dwf_mcp.devices.ad3 import (
    AD3_ANALOG_IN_PINS,
    AD3_ANALOG_OUT_PINS,
    AD3_DIO_PINS,
    AD3_RESOURCE_GROUPS,
    AD3_SUPPLY_PINS,
    AD3_TRIGGER_PINS,
)
from dwf_mcp.policy import SafetyPolicy
from dwf_mcp.registry import InstrumentRegistry

log = logging.getLogger(__name__)


def _build_backend(name: str) -> DwfBackend:
    if name == "fake":
        return FakeBackend()
    if name == "pydwf":
        # Imported lazily so unit tests don't require pydwf to import the module.
        from dwf_mcp.backends.pydwf_backend import PydwfBackend
        return PydwfBackend()
    raise ValueError(f"unknown backend {name!r}")


def _all_pins() -> list[str]:
    return [
        *AD3_DIO_PINS, *AD3_ANALOG_IN_PINS, *AD3_ANALOG_OUT_PINS,
        *AD3_SUPPLY_PINS, *AD3_TRIGGER_PINS,
    ]


class DwfMcpApp:
    """Holds the device, registry, and tool dispatch. Tests call `call_tool` directly;
    production wires this up to the MCP SDK stdio transport in `main()`."""

    def __init__(self, device: DwfDevice, registry: InstrumentRegistry) -> None:
        self.device = device
        self.registry = registry
        self._tools: dict[str, Any] = {
            "waveforms.open": self._tool_open,
            "waveforms.close": self._tool_close,
            "waveforms.status": self._tool_status,
            "waveforms.list_pins": self._tool_list_pins,
        }

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            self.device.tick_idle()
            return await self._tools[name](**args)
        except KeyError:
            raise ValueError(f"unknown tool {name!r}") from None

    async def _tool_open(self, **kwargs: Any) -> dict[str, Any]:
        policy_fields = {
            f: kwargs.pop(f) for f in [
                "supply_max_voltage_pos", "supply_max_voltage_neg", "supply_max_current",
                "awg_max_amplitude", "pattern_voltage", "require_explicit_enable",
            ] if f in kwargs
        }
        if policy_fields:
            self.device.policy = SafetyPolicy(**policy_fields)
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
        self.device.close()
        return {"closed": True}

    async def _tool_status(self) -> dict[str, Any]:
        return self.device.status()

    async def _tool_list_pins(self) -> dict[str, Any]:
        return {
            "all_pins": _all_pins(),
            "claimed": self.device.allocator.claimed_pins(),
            "resource_groups": [
                {"name": g.name, "pins": sorted(g.pins), "exclusive": g.exclusive}
                for g in self.device.allocator.resource_groups
            ],
        }


def build_app(
    backend_name: str | None = None,
    workspace: str | None = None,
    idle_timeout_s: float = 600.0,
) -> DwfMcpApp:
    backend_name = backend_name or os.environ.get("DWF_BACKEND", "pydwf")
    backend = _build_backend(backend_name)
    allocator = PinAllocator(resource_groups=AD3_RESOURCE_GROUPS)
    device = DwfDevice(
        backend=backend,
        policy=SafetyPolicy(),
        allocator=allocator,
        workspace=workspace or "",
        idle_timeout_s=idle_timeout_s,
    )
    registry = InstrumentRegistry()
    return DwfMcpApp(device, registry)


def main() -> None:
    """Stdio MCP transport entry point. Wires DwfMcpApp into the mcp SDK."""
    logging.basicConfig(level=logging.INFO)
    from mcp.server import Server  # imported lazily
    from mcp.server.stdio import stdio_server

    app = build_app()
    server: Server = Server("dwf-mcp")

    @server.list_tools()
    async def _list_tools() -> list[dict[str, Any]]:
        return [{"name": name, "description": ""} for name in app._tools]  # noqa: SLF001

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        return await app.call_tool(name, arguments)

    async def _run() -> None:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
```

Note: the `tmp_path` argument in tests should be passed via `workspace=str(tmp_path)`, and `DwfDevice` accepts an empty-string workspace by falling through to its own default — but `ArtifactWriter` is the one that uses workspace. For this task, `DwfDevice` only stores the workspace for status reporting; the writer comes in stage 2. If `workspace` is empty, leave it as `""` for now; we'll wire `ArtifactWriter` in when instruments need it.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_server.py -v`
Expected: all 3 tests pass.

Run full unit + integration suite: `pytest -m "not hardware" -v`
Expected: all tests pass (policy + allocator + artifacts + registry + fake backend + device + server).

**Step 5: Commit**

```bash
git add src/dwf_mcp/server.py tests/integration/test_server.py
git commit -m "feat(server): MCP entry with waveforms.open/close/status/list_pins meta tools"
```

---

### Task 9: PydwfBackend + hardware smoke test

**Files:**
- Create: `src/dwf_mcp/backends/pydwf_backend.py`
- Create: `tests/hardware/test_pydwf_backend.py`

**Background:** Real backend on top of `pydwf`. Only enumerate/open/close/info for now; instrument-level methods will get added in stage 2 as each instrument is wired. Hardware test runs against a physically connected AD3 and is skipped by default via the `@pytest.mark.hardware` mechanism set up in `conftest.py`.

**Step 1: Write hardware-marked tests**

`tests/hardware/test_pydwf_backend.py`:
```python
from __future__ import annotations

import pytest


@pytest.mark.hardware
def test_real_ad3_enumerate_and_open() -> None:
    from dwf_mcp.backends.pydwf_backend import PydwfBackend

    backend = PydwfBackend()
    devices = backend.enumerate()
    assert any(d.model.startswith("Analog Discovery") for d in devices), devices

    info = backend.open()
    try:
        assert info.serial
        assert backend.is_open
    finally:
        backend.close()
    assert not backend.is_open
```

**Step 2: Verify hardware test is skipped by default**

Run: `pytest tests/hardware -v`
Expected: 1 skipped (the conftest skip kicks in unless `-m hardware` is given).

**Step 3: Implement `PydwfBackend`**

`src/dwf_mcp/backends/pydwf_backend.py`:
```python
from __future__ import annotations

import logging
from typing import Any

from dwf_mcp.backend import DeviceInfo, DwfBackend, DwfBackendError

log = logging.getLogger(__name__)


class PydwfBackend(DwfBackend):
    """Backend backed by pydwf / libdwf. Imported lazily so unit tests can avoid it."""

    def __init__(self) -> None:
        from pydwf import DwfLibrary  # type: ignore[import-not-found]
        self._dwf = DwfLibrary()
        self._device: Any | None = None
        self._info: DeviceInfo | None = None

    def enumerate(self) -> list[DeviceInfo]:
        enum = self._dwf.deviceEnum
        count = enum.enumerateDevices()
        out: list[DeviceInfo] = []
        for i in range(count):
            try:
                serial = enum.deviceSerialNumber(i)
                name = enum.deviceName(i)
            except Exception as exc:
                log.warning("failed to enumerate device %d: %s", i, exc)
                continue
            out.append(
                DeviceInfo(
                    serial=serial,
                    model=name,
                    firmware="",  # filled on open
                    sample_rate_max_hz=125_000_000,  # AD3 nominal; refine on open
                    dio_count=16,
                    analog_in_channels=2,
                    analog_out_channels=2,
                )
            )
        return out

    def open(self, serial: str | None = None) -> DeviceInfo:
        if self._info is not None:
            return self._info
        enum = self._dwf.deviceEnum
        count = enum.enumerateDevices()
        target_index: int | None = None
        for i in range(count):
            if serial is None or enum.deviceSerialNumber(i) == serial:
                target_index = i
                break
        if target_index is None:
            raise DwfBackendError(f"no Digilent device matches serial {serial!r}")
        device = self._dwf.deviceControl.open(target_index)
        try:
            firmware = self._dwf.deviceEnum.deviceVersion(target_index)
        except Exception:
            firmware = ""
        info = DeviceInfo(
            serial=enum.deviceSerialNumber(target_index),
            model=enum.deviceName(target_index),
            firmware=firmware,
            sample_rate_max_hz=125_000_000,
            dio_count=16,
            analog_in_channels=2,
            analog_out_channels=2,
        )
        self._device = device
        self._info = info
        return info

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception as exc:
                log.warning("error closing device: %s", exc)
            self._device = None
        self._info = None

    @property
    def is_open(self) -> bool:
        return self._info is not None
```

Note: the exact `pydwf` API names above (`deviceEnum.enumerateDevices`, `deviceControl.open`, etc.) need verification against the installed `pydwf` version. If method names differ, adjust to match the actual API — the structure (enumerate → match by serial → open → cache info → close) stays the same.

**Step 4: Run unit suite to confirm nothing broke**

Run: `pytest -m "not hardware" -v`
Expected: all tests still pass. The `pydwf` import is lazy, so missing hardware won't break unit tests.

**Step 5: Run hardware smoke test (only if AD3 is plugged in)**

Run: `pytest tests/hardware -m hardware -v`
Expected: 1 passing test if an AD3 is connected; meaningful error otherwise. If pydwf method names differ from above, fix and re-run.

**Step 6: Commit**

```bash
git add src/dwf_mcp/backends/pydwf_backend.py tests/hardware/test_pydwf_backend.py
git commit -m "feat(backend): pydwf-backed PydwfBackend + hardware smoke test"
```

---

### Task 10: End-of-stage verification

**Step 1: Full test sweep**

Run: `pytest -m "not hardware" -v`
Expected: ~30+ tests, all passing.

Run: `ruff check .`
Expected: no issues.

Run: `mypy src/dwf_mcp`
Expected: no errors (or document any noisy false positives).

**Step 2: Hardware sweep (if AD3 available)**

Run: `pytest -m hardware -v`
Expected: 1 passing test.

**Step 3: Update README with status**

Append to `README.md`:
```markdown
## Status

Foundation complete (stage 1 of N):
- Safety policy, pin allocator, artifact writer
- DwfBackend ABC + fake + pydwf backends
- DwfDevice session with lazy open / idle timeout / unplug recovery
- MCP server with `waveforms.open`/`close`/`status`/`list_pins`
- AD3 pin map (provisional — confirm against reference manual before stage 2)

Stage 2: scope + supply + i2c vertical slice.
Stage 3: remaining instruments (logic, awg, pattern, dio, dmm, can, spi, uart).
Stage 4: passive decoders.
```

Commit:
```bash
git add README.md
git commit -m "docs: foundation status in README"
```

**Step 4: Final commit pass — ensure clean working tree**

Run: `git status`
Expected: clean.

---

## Out of scope (saved for next plan)

- Any actual instrument behavior (scope.capture, supply.set, i2c.write, etc.)
- VCD writer
- Decoder modules
- Recording / streaming modes
- Trigger I/O
- System monitor
- Idle-timeout background task (currently caller-driven via `tick_idle`; the stdio loop will call it between requests — that's enough)

## Open questions to resolve during execution

- **AD3 pin map / resource groups**: provisional values in `devices/ad3.py` need confirming against the AD3 hardware reference manual before stage 2 wires real instruments. If the manual shows different shared-clock domains, update `AD3_RESOURCE_GROUPS` accordingly.
- **`pydwf` API surface**: method names in `pydwf_backend.py` assume a specific `pydwf` version. Verify against the installed version's docs and adjust.
- **`mcp` SDK shape**: tool registration via `@server.list_tools` / `@server.call_tool` is the SDK 1.x API. If the installed `mcp` version differs, adjust `main()` accordingly. The `DwfMcpApp.call_tool` shim is testable without the SDK so this risk is isolated.
