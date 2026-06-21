# Frequency-Domain Recipes — spectrum · bode · impedance

These three instruments are **analog-only** (AD3, ADP2230) — they all require both an AWG output and two analog-in channels, and they are capability-gated out on the Digital Discovery. All three reuse the same coherent single-tone sweep DSP from `sweep_dsp.py`.

See `dwf://cookbook/bench` for ground-topology rules and hardware gotchas referenced below.

---
id: freq-domain:spectrum
tools: [waveforms.open, waveforms.list_pins, spectrum.configure, spectrum.measure, spectrum.transform, awg.configure, awg.start, awg.stop]
---

## Spectrum: FFT of a Single Analog Channel

**Goal / when to use:** Capture one analog channel and compute its frequency spectrum (FFT). Use to measure the frequency content of a signal — identify tones, measure THD/SNR, view the noise floor.

**Preflight:** `waveforms.open` (analog device — AD3 or ADP2230); `waveforms.list_pins` to confirm `scope{channel}` is free. The `spectrum` instrument will claim the analog-in channel under its own name (not `"scope"`), so a concurrent `scope.capture` on the same channel will be blocked.

**Wiring:**

- Connect `CH1_P` (scope CH1 plus) to the signal under test.
- Connect `CH1_NEG` to the signal ground. (See bench reference — differential input ground rule.)
- Connect `GND` to the circuit ground.
- For a self-test with the AWG: connect `W1` → `CH1_P`, `GND` → `CH1_NEG`. Use a short jumper wire or a BNC T-piece.

**Tools + sequence:**

1. (If driving stimulus) `awg.configure` with `function: "Sine"`, `frequency_hz`, `amplitude_v` (amplitude is peak). Then `awg.start`.
2. `spectrum.configure` — set `channel` (1 or 2), `sample_rate_hz`, `buffer_size`, `window` (`"hann"` default; `"flattop"` for amplitude accuracy; `"blackman"` for dynamic range; `"rectangular"` for coherent captures). Key relationships: Nyquist = `sample_rate_hz / 2`, bin resolution RBW = `sample_rate_hz / buffer_size`. `amplitude` (`"rms"` default or `"peak"`) sets the magnitude convention. `averaging` averages N captures in the power domain for noise reduction.
3. `spectrum.measure` — acquires via AnalogIn and returns an NPZ artifact containing `frequency_hz`, `magnitude_v` (Vrms or Vpeak per `amplitude` mode), `magnitude_dbv` arrays, and a summary with `peak_frequency_hz`, `peak_magnitude_dbv`, `dc_magnitude_dbv`, `noise_floor_dbv`, `rbw_hz`, `enbw_hz`.
4. (Alternative to `measure`) `spectrum.transform` — FFT an existing scope NPZ without re-acquiring. Use to re-analyze a prior waveform with a different window function.
5. `awg.stop` when stimulus is no longer needed.

**Formulae:**

- **THD** (total harmonic distortion): `formulae.thd(result, fundamental_hz, n_harmonics=5)` — returns `sqrt(Σ V_n²) / V_1`. Uses nearest-bin amplitudes from the `SpectrumResult`; harmonics beyond Nyquist are skipped. Status: **verified (fake-oracle; square-wave hardware confirmation pending)**.
- **SNR**: `formulae.snr_db(result, fundamental_hz)` — returns `20·log10(V_1 / noise_rms)` where noise is the RMS of all non-DC bins excluding the ±1-bin neighborhood of the fundamental. Status: **verified (fake-oracle; square-wave hardware confirmation pending)**.
- **Amplitude convention:** `amplitude="rms"` → a 1 V-peak AWG sine reads `peak_magnitude_dbv ≈ −3.0 dBV` (0.707 Vrms). `amplitude="peak"` → same tone reads `≈ 0 dBV`. The two differ by 20·log10(√2) = 3.0103 dB. Both modes record the convention in the sidecar so the result is self-describing.
- **Peak accuracy:** the `peak_frequency_hz` and `peak_magnitude_dbv` in the summary are **3-bin parabolic-interpolated** — accurate for off-bin tones, not just bin-centered ones. The per-bin `magnitude_dbv` array itself is raw (worst-case scalloping with `hann`; use `flattop` for accurate per-bin amplitude across the array).

**Interpretation:**

- `peak_frequency_hz` and `peak_magnitude_dbv` are the primary read-out for a single tone. For multi-tone signals, load the NPZ and inspect `magnitude_dbv` vs `frequency_hz`.
- `noise_floor_dbv` is a per-bin median — it is RBW/ENBW-dependent. A narrower RBW (larger `buffer_size` or lower `sample_rate_hz`) lowers the per-bin floor. A normalized dBV/√Hz density is a deferred fast-follow.
- `dc_magnitude_dbv` is reported separately and excluded from the peak search.

**Gotchas:**

- **Stale-first-AnalogIn-buffer:** `spectrum.measure` discards one warm-up acquisition automatically. If the first result still looks ~19 dB low (indicating the warm-up was not effective — e.g. the device was just opened), call `spectrum.measure` a second time.
- **Window choice:** use `flattop` when accurate amplitude is the goal (per-bin scalloping ≤ 0.02 dB). Use `hann` (default) for general-purpose. Use `rectangular` only for coherent captures where the tone falls exactly on a bin — non-coherent tones with `rectangular` will show severe spectral leakage.
- **Buffer size cap:** `spectrum.configure` validates `buffer_size` against `DeviceInfo.analog_in_buffer_max` (returned by `waveforms.open`). Requesting a buffer larger than the cap raises an error immediately.
- **`spectrum` claims the analog-in channel under `"spectrum"`, not `"scope"`** — a live `scope.capture` on the same channel will get a `PinAllocationError`. Release spectrum first.

---
id: freq-domain:filter-response
tools: [waveforms.open, waveforms.list_pins, bode.configure, bode.measure]
---

## Bode: Filter Frequency Response (Gain + Phase)

**Goal / when to use:** Sweep a sine stimulus across frequency and measure a DUT's transfer function — gain (dB) and phase (degrees) vs. frequency. Use to characterize filters, amplifiers, and other two-port networks. The measurement is ratiometric (CH1 = Vin, CH2 = Vout), so AWG amplitude accuracy is irrelevant — only the DUT's response survives the ratio.

**Preflight:** `waveforms.open` (analog device with at least 1 AWG channel — AD3 or ADP2230); `waveforms.list_pins` to confirm `awg1`, `scope1`, and `scope2` pins are free. `bode.measure` claims all three for the duration of the sweep.

**Wiring:**

```
W1 ──── DUT input (also CH1_P = Vin reference)
DUT output ──── CH2_P (Vout)
CH1_NEG, CH2_NEG ──── signal GND
GND ──── circuit GND
```

For an RC low-pass self-test (1 kΩ + 0.1 µF, f_c ≈ 1591.5 Hz):
- `W1` → one end of R (1 kΩ) and `CH1_P`
- Other end of R → one end of C and `CH2_P`
- Other end of C and `CH1_NEG` and `CH2_NEG` → GND

**Tools + sequence:**

1. `bode.configure` — required params: `start_hz`, `stop_hz`, `points`. Key optional params: `spacing` (`"log"` default, or `"linear"`), `amplitude_v` (AWG drive amplitude, default 0.5 V; reduce for sensitive DUTs), `drive_channel` (default 1 = W1), `ref_channel` (CH1 = Vin reference, default 1), `dut_channel` (CH2 = Vout, default 2), `range_v` (scope range, default 5.0 V), `samples_per_cycle` (default 64), `min_cycles` (default 16), settle parameters (`settle_cycles`, `settle_min_s`, `settle_s`).
2. `bode.measure` — runs the entire sweep as one blocking call. Returns an NPZ artifact with per-point columns: `frequency_hz` (actual hardware readback, not requested), `gain_db`, `phase_deg`, `vin_rms`, `vout_rms`, `achieved_cycles`, `samples_per_cycle`, `coherence_error_cycles`, `quality_flags`, `clipping_flag`. The summary reports `point_count`, actual `start_hz`/`stop_hz`, `gain_db_min/max`, `phase_deg_min/max`, `flagged_points`, `clipped_points`.

**Formulae:**

- **−3 dB frequency:** `formulae.bode_f3db(freq_hz, gain_db)` — from the sweep data, finds the frequency where gain drops 3.0103 dB below the passband (gain at the lowest sweep point), using log-frequency interpolation. Returns `{"f_3db_hz": float | None, "rolloff_db_per_decade": float}`. Returns `f_3db_hz=None` if gain never drops 3 dB in the swept range. Status: **verified** (hardware-validated on a 1 kΩ + 0.1 µF RC low-pass: −3.09 dB / −42.3° measured at f_c = 1591.5 Hz, −20 dB/decade rolloff confirmed on AD3).
- **Bandwidth / Q:** for resonant (bandpass/bandstop) filters, bandwidth is the frequency span between the two −3 dB points and Q = f_center / BW. Compute from the NPZ arrays directly. Status: **textbook (no resonant hardware oracle)**.

**Interpretation:**

- For a first-order low-pass: passband gain at low frequencies (ideally 0 dB through-wire, or the DUT's insertion gain), −3.01 dB at f_c, −20 dB/decade rolloff, phase approaching −90° at high frequencies.
- `gain_db` is `20·log10(|Vout|/|Vin|)`. Phase is `angle(Vout/Vin)` wrapped to (−180°, 180°]; negative phase = lag (correct for a low-pass).
- `vin_rms` and `vout_rms` are absolute Vrms — use them to sanity-check that the drive level was appropriate. Very low `vin_rms` can indicate a wiring fault.
- `quality_flags` is a bitmask. Non-zero flags (e.g. `low_cycles`, `noncoherent`, `near_nyquist`) do not always invalidate a point, but they indicate reduced confidence. `clipped_points > 0` means the drive amplitude was too high — reduce `amplitude_v`.
- `frequency_hz` in the NPZ is the **actual hardware readback** for each point, not the requested grid. Quantization can produce slight grid irregularities; this is by design.

**Gotchas:**

- **Settle-before-arm:** the Bode sweep internally settles *before* arming the scope on each point. The settle time per point is `max(settle_cycles/freq, settle_min_s)`. If your DUT has a long settling tail (high-Q resonance, large time constant), increase `settle_s` (absolute seconds; replaces the `settle_cycles/freq` term but is still floored by `settle_min_s`).
- **Stale-first-AnalogIn-buffer:** `bode.measure` discards one warm-up acquisition at the start of the sweep.
- **`bode` claims AWG + all scope channels** under `"bode"` for the full sweep duration. Do not interleave other scope or AWG calls.
- **Coherent acquisition:** at each sweep point the server targets an integer number of signal cycles in the acquisition buffer (coherent capture). If the hardware cannot honor the exact rate/buffer, the achieved values may differ from the request — the point is flagged `noncoherent` if the fractional-cycle error exceeds tolerance. A few `noncoherent` points in a large sweep are normal at awkward frequencies; a sweep full of them suggests a backend configuration issue.
- **Digital Discovery:** `bode.configure` raises `InstrumentNotConfigured` on the Digital Discovery.

---
id: freq-domain:impedance
tools: [waveforms.open, waveforms.list_pins, impedance.configure, impedance.measure]
---

## Impedance: Complex Z(f) of a DUT

**Goal / when to use:** Sweep a sine stimulus and measure a DUT's complex impedance — `|Z|`, phase, resistance, reactance, and derived series-equivalent C/L/Q/D — vs. frequency. Use to characterize capacitors (measure ESR + Cs), inductors (measure DCR + Ls), resonant networks (find SRF), and unknown passive DUTs.

**Preflight:** `waveforms.open` (AD3 or ADP2230); `waveforms.list_pins` to confirm `awg1`, `scope1`, `scope2` free. Choose a series reference resistor `R_ref` — accuracy is best when `R_ref ≈ |Z|` of the DUT at the frequencies of interest.

**Wiring:**

```
W1 ──┬── R_ref ──┬── DUT ──── GND
     │           │
   CH1_P       CH2_P
 (V_total)    (V_dut)
 CH1_NEG and CH2_NEG ──── GND
 GND ──── circuit GND
```

- `R_ref` is a **precision** resistor (1% or better). Supply its actual measured value in `r_ref`.
- CH1 measures `V_total` (the W1 node — across the whole series network).
- CH2 measures `V_dut` (the junction between `R_ref` and the DUT — across the DUT only).
- The series current flows through `R_ref` and the DUT together: `I = (V_total − V_dut) / R_ref`.
- DUT impedance: `Z = V_dut · R_ref / (V_total − V_dut)` (all complex phasor quantities; AWG amplitude error cancels in the ratio).

Typical `R_ref` values: 100 Ω for low-Z DUTs (shorts, small capacitors at high frequency), 1 kΩ for mid-range, 10 kΩ for high-Z.

**Tools + sequence:**

1. `impedance.configure` — required params: `start_hz`, `stop_hz`, `points`, `r_ref` (Ω, required — your physical reference resistor value). Key optional params: `spacing` (`"log"` default), `amplitude_v` (AWG drive, default 0.5 V), `drive_channel` (default 1 = W1), `ref_channel` (CH1 = V_total, default 1), `dut_channel` (CH2 = V_dut, default 2), `range_v`, settle parameters.
2. `impedance.measure` — runs the full sweep, returns an NPZ artifact with per-point columns: `frequency_hz` (actual readback), `impedance_ohms` (`|Z|`), `phase_deg`, `resistance_ohms` (`Re Z`), `reactance_ohms` (`Im Z`), `capacitance_f`, `inductance_f`, `q_factor`, `dissipation`, `v_total_rms`, `v_dut_rms`, `achieved_cycles`, `samples_per_cycle`, `coherence_error_cycles`, `quality_flags`, `clipping_flag`. The sidecar carries `r_ref` and the quality-flag bit-to-name map.

**Formulae:**

- **Series-equivalent component values** are computed per-point from `Z = R + jX`:
  - `capacitance_f = −1/(2π·f·X)` when `X < 0` (capacitive), else `nan`.
  - `inductance_f = X/(2π·f)` when `X > 0` (inductive), else `nan`.
  - `q_factor = |X|/R`, `dissipation = R/|X|` — `nan` when `R ≤ 0` (measurement noise can push the recovered R negative) or when the denominator is 0.
  - These are **series-equivalent** values (Cs/Ls model). For a parallel-model DUT, series-equivalent C/L differs from the physical part; parallel-equivalent derivation from admittance is a deferred fast-follow.
- **SRF (series resonant frequency):** the frequency where `|reactance_ohms|` is minimum (phase ≈ 0° for a series-resonant component) or `|impedance_ohms|` is minimum. Read from the NPZ array — no dedicated formula. Status: **textbook (no resonant hardware oracle)**.
- **Parallel-equivalent component values:** from admittance `Y = G + jB`. Deferred — v1 reports series-equivalent only. Status: **textbook (deferred)**.

**Interpretation:**

- **Pure resistor:** `|Z|` flat vs. frequency, `phase_deg ≈ 0°`, `resistance_ohms ≈ R`, `reactance_ohms ≈ 0`. A validated 1 kΩ DUT reads flat ~1 kΩ across the sweep.
- **Ceramic capacitor:** `|Z|` decreases at −20 dB/decade, `phase_deg ≈ −90°`, `capacitance_f ≈` marked value. At SRF the capacitor's series inductance resonates — `|Z|` hits a minimum, phase passes through 0°, then `|Z|` increases inductive (+20 dB/decade) above SRF. ESR = `resistance_ohms` at SRF.
- **Inductor:** `|Z|` increases at +20 dB/decade, `phase_deg ≈ +90°`, `inductance_f ≈` value. DCR = `resistance_ohms` at low frequency.
- `quality_flags`: three impedance-specific flags indicate conditioning problems:
  - **`low_drive`**: `|V_total − V_dut|` (current signal across R_ref) is too small → DUT is **high-Z relative to R_ref** (`|Z| ≫ R_ref`). Remedy: increase `R_ref`.
  - **`low_dut_voltage`**: `|V_dut|` is too small → DUT is **low-Z relative to R_ref** (`|Z| ≪ R_ref`). Remedy: decrease `R_ref`.
  - **`ref_mismatch`**: supplementary guidance — recovered `|Z|/R_ref` is outside roughly [0.01, 100]. Use `low_drive` / `low_dut_voltage` as the authoritative conditioning flags; `ref_mismatch` is a reminder to pick a better `R_ref`.

**Diagnostic runbook:**

1. **Dead CH2 / `low_dut_voltage` flag everywhere** — check the CH2 wiring to the R_ref/DUT junction. If CH2 is disconnected or shorted to GND, `V_dut = 0` and the recovered Z is indeterminate.
2. **Unexpectedly high phase at low frequency (appears high-pass)** — a series capacitor has crept into the R_ref leg (e.g. a DC-blocking cap, a coupling cap on a breadboard trace). The corner frequency is `1/(2π·R_ref·C_stray)`. Inspect the R_ref leg for unexpected capacitance.
3. **`|Z|` reads too high everywhere** — ground missing. Confirm CH1_NEG, CH2_NEG, and GND are all tied to the circuit ground.
4. **High-Z at high frequencies biased too low** — this is the known v1 accuracy limitation: the scope's input capacitance sits in parallel with the DUT and becomes significant at high frequencies for high-Z DUTs. v1 has no open/short compensation; this is the primary motivation for the deferred calibration step.

**Gotchas:**

- **No open/short/SOL compensation in v1.** For high-impedance DUTs at high frequencies, the scope input capacitance and fixture parasitics bias the reading. The `low_drive` flag fires when this is severe; moderate bias is silent. Use `R_ref ≈ |Z|` to keep both channels well-conditioned.
- **Settle-before-arm:** same as Bode — settle runs before arm on each point.
- **Stale-first-AnalogIn-buffer:** discarded automatically.
- **Digital Discovery:** `impedance.configure` raises `InstrumentNotConfigured`.
- **R_ref in the series leg:** a capacitor or inductor in the R_ref leg will shift phase systematically across the sweep. Use a metal-film resistor with low self-inductance; avoid carbon composition or wirewound types at high frequency.
