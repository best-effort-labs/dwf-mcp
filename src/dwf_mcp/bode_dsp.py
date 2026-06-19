"""Pure DSP for the Bode/network analyzer instrument — no hardware, no I/O.
Coherent-first single-bin extraction: a rectangular complex projection at the
EXACT drive frequency (no window). On a coherent capture this is the unbiased
DFT bin; off-coherent points are FLAGGED (assess_quality), not windowed."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_FLOOR = 1e-15  # amplitude/ratio floor so log10 never sees 0


def extract_tone(samples: np.ndarray, sample_rate_hz: float, freq_hz: float) -> complex:
    """Single-bin complex amplitude at exactly freq_hz, returned as a **Vrms phasor**
    (magnitude = Vrms, angle = phase, cosine reference). Rectangular projection:
    (2/N)*sum x[n]*exp(-j2*pi*f*n/fs) is the one-sided complex peak; /sqrt(2) -> Vrms."""
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n == 0:
        return 0j
    k = freq_hz / sample_rate_hz  # cycles per sample
    nn = np.arange(n)
    proj = np.dot(x, np.exp(-2j * np.pi * k * nn))
    peak = 2.0 * proj / n
    return complex(peak / np.sqrt(2.0))


def bode_point(vin: np.ndarray, vout: np.ndarray,
               sample_rate_hz: float, freq_hz: float) -> dict[str, float]:
    """gain_db = 20log10(|Vout|/|Vin|), phase_deg = angle(Vout/Vin) in (-180,180]
    (negative = lag). Ratiometric: AWG amplitude error and the absolute timing/phase
    reference cancel. Vrms values are absolute (for guardrails)."""
    h_in = extract_tone(vin, sample_rate_hz, freq_hz)
    h_out = extract_tone(vout, sample_rate_hz, freq_hz)
    vin_rms = float(abs(h_in))
    vout_rms = float(abs(h_out))
    if vin_rms <= _FLOOR:
        # Reference is dead: gain undefined. Floor it; the caller flags low_vin_rms.
        return {"gain_db": 20.0 * np.log10(_FLOOR), "phase_deg": 0.0,
                "vin_rms": vin_rms, "vout_rms": vout_rms}
    ratio = h_out / h_in
    gain_db = float(20.0 * np.log10(max(abs(ratio), _FLOOR)))
    phase_deg = float(np.degrees(np.angle(ratio)))  # np.angle -> (-pi, pi] -> (-180, 180]
    return {"gain_db": gain_db, "phase_deg": phase_deg,
            "vin_rms": vin_rms, "vout_rms": vout_rms}


# ---------------------------------------------------------------------------
# Quality-flag bits (documented in the artifact sidecar via QF_NAMES).
# ---------------------------------------------------------------------------
QF_LOW_CYCLES = 1 << 0
QF_LOW_SAMPLES_PER_CYCLE = 1 << 1
QF_VERY_LOW_SAMPLES_PER_CYCLE = 1 << 2
QF_LOW_OVERSAMPLING = 1 << 3
QF_NEAR_NYQUIST = 1 << 4
QF_BUFFER_LIMITED = 1 << 5
QF_SAMPLE_RATE_LIMITED = 1 << 6
QF_NONCOHERENT = 1 << 7
QF_LOW_VIN_RMS = 1 << 8
QF_CLIPPED = 1 << 9

QF_NAMES: dict[int, str] = {
    QF_LOW_CYCLES: "low_cycles",
    QF_LOW_SAMPLES_PER_CYCLE: "low_samples_per_cycle",
    QF_VERY_LOW_SAMPLES_PER_CYCLE: "very_low_samples_per_cycle",
    QF_LOW_OVERSAMPLING: "low_oversampling",
    QF_NEAR_NYQUIST: "near_nyquist",
    QF_BUFFER_LIMITED: "buffer_limited",
    QF_SAMPLE_RATE_LIMITED: "sample_rate_limited",
    QF_NONCOHERENT: "noncoherent",
    QF_LOW_VIN_RMS: "low_vin_rms",
    QF_CLIPPED: "clipped",
}

# Thresholds (see spec). samples/cycle is two-tier; coherence tolerance is in cycles.
_SPC_WARN = 20.0
_SPC_HARD = 10.0
_OVERSAMPLE_RATIO = 10.0   # freq > fs/10 -> low_oversampling
_NYQUIST_RATIO = 0.4       # freq >= 0.4*fs -> near_nyquist
COHERENCE_TOL_CYCLES = 0.05


@dataclass
class AcqPlan:
    sample_rate_hz: float
    buffer_size: int
    cycles: float            # requested integer-cycle target (may be < min_cycles if clamped)
    samples_per_cycle: float
    clamp_flags: int         # buffer_limited / sample_rate_limited from device caps


def plan_acquisition(freq_hz: float, max_sample_rate_hz: float, max_buffer: int,
                     samples_per_cycle: float = 64.0, min_cycles: int = 16) -> AcqPlan:
    """Size a COHERENT (integer-cycle) capture for one tone, clamped to device caps.
    Returns the requested plan + the clamp flags. The ACHIEVED metrics are recomputed
    from hardware readbacks by the instrument (assess_quality), never from this."""
    if freq_hz <= 0:
        raise ValueError(f"freq_hz must be > 0, got {freq_hz}")
    flags = 0
    sr = freq_hz * samples_per_cycle
    if max_sample_rate_hz and sr > max_sample_rate_hz:
        sr = max_sample_rate_hz
        flags |= QF_SAMPLE_RATE_LIMITED
    spc = sr / freq_hz
    buffer = int(round(min_cycles * spc))
    if max_buffer and buffer > max_buffer:
        buffer = max_buffer
        flags |= QF_BUFFER_LIMITED
    buffer = max(buffer, 16)
    # cycles is derived from the FINAL buffer so the AcqPlan is always self-consistent
    # (buffer_size == cycles * samples_per_cycle), even after a clamp or the 16-sample floor.
    cycles = buffer / spc
    return AcqPlan(sample_rate_hz=sr, buffer_size=buffer, cycles=cycles,
                   samples_per_cycle=spc, clamp_flags=flags)


def assess_quality(achieved_cycles: float, samples_per_cycle: float,
                   freq_hz: float, sample_rate_hz: float, min_cycles: int,
                   coherence_tol: float = COHERENCE_TOL_CYCLES) -> tuple[int, float]:
    """Per-point measurement-quality flags computed from ACTUAL (readback) values.
    Returns (flags, coherence_error_cycles)."""
    flags = 0
    coh_err = abs(achieved_cycles - round(achieved_cycles))
    if coh_err > coherence_tol:
        flags |= QF_NONCOHERENT
    if achieved_cycles < min_cycles:
        flags |= QF_LOW_CYCLES
    if samples_per_cycle < _SPC_HARD:
        flags |= QF_VERY_LOW_SAMPLES_PER_CYCLE
    elif samples_per_cycle < _SPC_WARN:
        flags |= QF_LOW_SAMPLES_PER_CYCLE
    if freq_hz > sample_rate_hz / _OVERSAMPLE_RATIO:
        flags |= QF_LOW_OVERSAMPLING
    if freq_hz >= _NYQUIST_RATIO * sample_rate_hz:
        flags |= QF_NEAR_NYQUIST
    return flags, float(coh_err)


def frequency_grid(start_hz: float, stop_hz: float, points: int,
                   spacing: str = "log") -> np.ndarray:
    if points < 2:
        raise ValueError(f"points must be >= 2, got {points}")
    if spacing == "log":
        if start_hz <= 0 or stop_hz <= 0:
            raise ValueError("log spacing requires start_hz, stop_hz > 0")
        return np.logspace(np.log10(start_hz), np.log10(stop_hz), points)
    if spacing == "linear":
        return np.linspace(start_hz, stop_hz, points)
    raise ValueError(f"spacing must be 'log' or 'linear', got {spacing!r}")


def detect_clip(samples: np.ndarray, range_v: float) -> bool:
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0:
        return False
    return bool(np.max(np.abs(x)) > 0.98 * range_v)
