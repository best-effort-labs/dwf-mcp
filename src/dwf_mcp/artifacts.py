from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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

        np.savez_compressed(output_path, **arrays)  # type: ignore[arg-type]
        sidecar_path = output_path.with_suffix(".json")
        sidecar = {
            "instrument": instrument,
            "captured_at": datetime.now(UTC).isoformat(),
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

    def write_parquet(
        self,
        instrument: str,
        records: list[dict[str, Any]],
        config: dict[str, Any],
        output_path: Path | None = None,
        description: str | None = None,
    ) -> ArtifactResult:
        import pyarrow as pa
        import pyarrow.parquet as pq

        if output_path is None:
            output_path = self._default_path(instrument, ".parquet")
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        # Empty table has no column schema; callers should not attempt to
        # concatenate empty and non-empty parquet files from the same instrument.
        table = pa.Table.from_pylist(records) if records else pa.table({})
        pq.write_table(table, output_path)  # type: ignore[no-untyped-call]  # pyarrow untyped

        sidecar_path = output_path.with_suffix(".json")
        sidecar = {
            "instrument": instrument,
            "captured_at": datetime.now(UTC).isoformat(),
            "description": description,
            "config": config,
            "summary": {"count": len(records)},
        }
        sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str))
        return ArtifactResult(
            path=str(output_path),
            sidecar_path=str(sidecar_path),
            summary={"count": len(records)},
        )

    def _default_path(self, instrument: str, suffix: str) -> Path:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        name = f"{ts}_{instrument}_{uuid.uuid4().hex[:8]}{suffix}"
        return self.workspace / "captures" / name
