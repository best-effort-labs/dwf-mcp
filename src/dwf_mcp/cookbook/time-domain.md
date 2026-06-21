# Time-Domain Recipes — scope · awg · supply · dmm · dio · logic · pattern

Waveform capture, stimulus generation, power, DC measurement, digital I/O, long streaming records, and pattern generation. Analog instruments (scope, awg, supply, dmm) are available on analog devices (AD3, ADP2230) only. Digital instruments (logic, pattern, dio) are available on all devices including the Digital Discovery.

See `dwf://cookbook/bench` for ground topology, capability gating, and hardware gotchas.

---
id: time-domain:acquire-waveform
tools: [waveforms.open, scope.configure, scope.set_trigger, scope.capture, awg.configure, awg.start, awg.stop]
---

## Acquire a Waveform (Triggered Scope Capture)

**Goal / when to use:** Capture one or two analog channels triggered on a signal edge. The standard path for oscilloscope-style time-domain waveforms — measure rising/falling edges, pulse widths, overshoot, or any repeating signal.

**Preflight:** `waveforms.open` (analog device); confirm scope channel(s) are free with `waveforms.list_pins`.

**Wiring:**

- Connect `CH1_P` to the signal under test; `CH1_NEG` to signal ground.
- Connect `GND` to circuit ground.
- For AWG stimulus: `W1` → signal node, `GND` → circuit GND. With Jumperless routing, allow ≥ 0.5 s for RC settling before the first capture.

**Tools + sequence:**

1. (Optional stimulus) `awg.configure` with `function`, `frequency_hz`, `amplitude_v` (peak), `offset_v`. Then `awg.start`.
2. `scope.configure` — set `channel` (1 or 2), `range_v` (half the full-scale range in either direction), `offset_v`, `coupling` (`"DC"` or `"AC"`). Configure both channels for a two-channel capture by calling `scope.configure` twice.
3. `scope.set_trigger` — set `source` (`"channel1"`, `"channel2"`, `"external"`, or `"none"`), `level_v` (threshold voltage), `slope` (`"rising"` or `"falling"`), and `auto` (`true` for free-run on timeout, `false` to wait indefinitely). A trigger on `"channel1"` at `level_v=1.65` with `slope="rising"` is a typical digital-signal trigger.
4. `scope.capture` — pass `sample_rate_hz` and `n_samples`; optionally `output_path`. Returns an NPZ artifact path, sidecar path, and a summary (sample count, time span, per-channel min/max/mean/RMS).
5. `awg.stop` when done.

**Formulae:** None (time-domain arrays — compute rise time, pulse width, overshoot from the NPZ `voltage` arrays and `time_s` index directly).

**Interpretation:**

- The NPZ contains `time_s` (the time axis), and per-channel `voltage` arrays. The sidecar carries the configuration.
- Triggered captures align the trigger event at the pre-trigger position (default: ~10% of the buffer, configurable). With `auto: true`, a trigger that does not fire within the timeout will still return a capture (free-run fallback).
- With `source: "none"` the capture is immediate free-run with no trigger alignment.

**Gotchas:**

- **Ground topology:** `CH1_NEG` and `CH2_NEG` must be connected to the signal ground — the AD3 inputs are true differential and float otherwise.
- **Stale-first-AnalogIn-buffer:** for triggered captures, the trigger-wait masks the stale buffer. For free-run (`source: "none"`), if the first reading is ~19 dB too low, capture a second time.
- **Jumperless RC settling (~125 ms):** add ≥ 0.5 s wait after routing changes before capturing through the Jumperless.

---
id: time-domain:generate-stimulus
tools: [waveforms.open, awg.configure, awg.start, awg.stop, awg.upload_custom]
---

## Generate an AWG Stimulus

**Goal / when to use:** Drive a known waveform from the AWG output to stimulate a DUT or provide a reference signal. Use `awg.configure` for standard functions; use `awg.upload_custom` for arbitrary waveforms from a .npy file.

**Preflight:** `waveforms.open` (analog device); `waveforms.list_pins` to confirm `awg{channel}` is free.

**Wiring:**

- `W1` → DUT signal input (or scope CH1_P for a self-test).
- `GND` → circuit GND and scope CH1_NEG.

**Tools + sequence:**

For standard waveforms:

1. `awg.configure` — params: `channel` (1 for W1, 2 for W2 on AD3), `function` (`"Sine"`, `"Square"`, `"Triangle"`, `"RampUp"`, `"RampDown"`, `"DC"`, `"Noise"`), `frequency_hz`, `amplitude_v` (**peak** amplitude — not peak-to-peak, not RMS), `offset_v` (DC offset, default 0).
2. `awg.start` — arms and begins generating on the hardware output pin.
3. `awg.stop` — silences the output; configuration is preserved.

For arbitrary waveforms:

1. Prepare a 1-D float64 .npy file with samples in [-1.0, 1.0]. The AWG scales by `amplitude_v`.
2. `awg.upload_custom` — params: `channel`, `samples_npy_path`, `amplitude_v`. Returns `{"uploaded": true, "n_samples": N}`.
3. `awg.start` to begin playback (loops indefinitely until `awg.stop`).

**Formulae:** `amplitude_v` is peak (one-sided). For a sine: Vpp = 2 × `amplitude_v`; Vrms = `amplitude_v` / √2. A 1 V-peak sine = 0.707 Vrms → reads −3.0 dBV in `spectrum.measure` with `amplitude="rms"`, or 0 dBV with `amplitude="peak"`.

**Interpretation:** The AWG output starts immediately on `awg.start`. The waveform loops continuously until `awg.stop` or `waveforms.close`. For a repeating pattern (arbitrary waveform), the repetition rate is set by the AWG's internal sample clock — configure that separately via a second `awg.configure` call if a specific playback rate is needed.

**Gotchas:**

- **ADP2230:** only W1 is a user output. W2 and W3 are reported by the SDK but are internal — `awg.configure` with `channel=2` will raise `PinAllocationError` on ADP2230.
- Every `awg.start` routes through `device.gate_output` and is logged to `dwf-safety.log`. If the safety policy rejects the amplitude (above `awg_max_amplitude_v` set at `waveforms.open`), a `SafetyViolation` error is returned.

---
id: time-domain:power-dut
tools: [waveforms.open, supply.set, supply.enable, supply.read, supply.disable]
---

## Power a DUT from the Programmable Supply

**Goal / when to use:** Bring up a DUT from the AD3's built-in ±5 V programmable supply rails (VPos / VNeg). Use to power a 3.3 V or 5 V circuit directly from the device without a bench supply.

**Preflight:** `waveforms.open` with `supply_max_voltage_pos` and/or `supply_max_voltage_neg` set to cap the maximum allowed voltage (safety policy). Example: `supply_max_voltage_pos=3.3` prevents accidentally setting 5 V on a 3.3 V circuit.

**Wiring:**

- `VPos` (positive rail output pin, labeled `V+`) → DUT VCC.
- `VNeg` (negative rail output pin, labeled `V-`) → DUT VSS (if needed; leave unconnected for single-supply).
- `GND` → DUT GND.

**Tools + sequence:**

1. `supply.set` — stage `channel` (`"vpos"` or `"vneg"`), `voltage` (volts), optional `current_limit_a`. This does **not** energize the rail.
2. `supply.enable` — energize the staged rail through the safety gate.
3. `supply.read` — read back live `voltage_v` and `current_a`. Use to verify the DUT is drawing expected current after power-up.
4. `supply.disable` — de-energize the rail. The voltage setpoint is preserved; `supply.enable` can restart it.

**Formulae:** None.

**Interpretation:**

- `supply.read` returns the live setpoint and measured current. A DUT drawing far more current than expected suggests a wiring fault or short.
- Supply accuracy is to within a few mV; use `dmm.measure` on a known precision load if you need a calibrated verification.

**Gotchas:**

- `supply.enable` is gated by `device.gate_output` — it is logged to `dwf-safety.log` and rejected if the voltage exceeds the policy cap set at `waveforms.open`. If no `supply_max_voltage_pos` was set, the hardware default limit applies.
- `waveforms.close` de-energizes the supply.

---
id: time-domain:measure-dc
tools: [waveforms.open, dmm.measure]
---

## Measure a DC Voltage (DMM)

**Goal / when to use:** Take a high-accuracy averaged DC or AC voltage measurement on one analog input channel. More accurate than a single scope sample because `dmm.measure` averages many readings.

**Preflight:** `waveforms.open` (analog device); `CH_NEG` connected to ground (ground topology rule).

**Wiring:** Same as scope — `CH1_P` → signal, `CH1_NEG` → signal GND, `GND` → circuit GND.

**Tools + sequence:**

1. `dmm.measure` — params: `channel` (1 or 2), `mode` (`"dc"` or `"ac"`), optional `n_samples` for averaging. Returns `mean_v`, `min_v`, `max_v`, `rms_v`.

**Formulae:** For AC: true RMS = `rms_v`. For a pure sine: `rms_v = amplitude / √2`. For DC plus ripple: `rms_v² = dc² + ac_rms²`.

**Interpretation:** `mean_v` is the averaged DC level. `min_v`/`max_v` show peak excursions within the averaging window. `rms_v` equals `mean_v` for a pure DC signal; for AC or DC+ripple it is always ≥ `|mean_v|`.

**Gotchas:** `dmm.measure` uses the AnalogIn engine — it is subject to the stale-first-buffer issue. For a one-shot DMM reading right after `waveforms.open`, call `dmm.measure` twice and use the second result if the first looks wrong.

---
id: time-domain:gpio
tools: [waveforms.open, waveforms.list_pins, dio.set_direction, dio.set, dio.read, dio.set_pull, dio.set_voltage, dio.set_drive]
---

## GPIO — Read / Drive Digital Pins

**Goal / when to use:** Control or read individual digital I/O pins. Use for bit-banging, status-LED toggling, reset signals, chip-select lines, or reading logic levels.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to identify available DIO pins and their current owners.

**Wiring:** Connect DIO pin(s) to the target signal. Observe the device's I/O voltage level (3.3 V default on AD3; adjustable on devices with `dio.set_voltage`).

**Tools + sequence:**

For output:
1. `dio.set_direction` — set `pin` (`"dio0"`, `"dio1"`, …) and `direction` (`"out"`).
2. `dio.set` — set `pin` and `value` (0 or 1).

For input:
1. `dio.set_direction` — set `direction` to `"in"` (high-impedance).
2. `dio.read` — returns the current logic level of the pin.

For pull resistors:
- `dio.set_pull` — set `pin` and `mode` (`"up"`, `"down"`, `"none"`, `"keeper"`). **ADP2230 bank-global pull caveat:** on the ADP2230, this applies to all 16 pins in the bank (see bench reference).

For adjustable I/O voltage (devices that support it):
- `dio.set_voltage` — set the DIO logic voltage level (e.g. `1.8` or `3.3`).

For drive strength (ADP2230):
- `dio.set_drive` — set `strength_ma` and `slew_rate` for the DIO bank.

**Formulae:** None.

**Interpretation:** `dio.read` returns 0 or 1. On an undriven input pin with no pull, the reading may be indeterminate (floating). Use `dio.set_pull` to add a pull-up or pull-down for stable reading.

**Gotchas:** `dio.set` routes through `device.gate_output` (logged, safety-gated). A pin must have `direction="out"` before `dio.set` — calling `dio.set` on an input pin raises an error.

---
id: time-domain:long-record
tools: [waveforms.open, scope.configure, scope.record_start, scope.record_status, scope.record_stop, logic.configure, logic.record_start, logic.record_status, logic.record_stop]
---

## Long Streaming Record (Scope or Logic)

**Goal / when to use:** Capture a signal for longer than the hardware buffer allows. `scope.record_start` / `logic.record_start` use the device's streaming record mode to write samples to disk continuously. Use for power-on sequences, long protocol captures, or any signal that is too long for a single buffer.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm scope/logic pins are free. Allocate disk space: at 1 MHz, a 2-second logic record with 4 pins is ~2 MB NPZ; scope at 1 MHz for 10 s is ~80 MB. For Jumperless-routed analog signals, note the ~125 ms RC settling.

**Wiring:** Same as triggered capture for scope; DIO pins connected to digital bus for logic.

**Tools + sequence (scope record):**

1. `scope.configure` — channel, range, coupling.
2. `scope.record_start` — params: `sample_rate_hz`, `duration_s` (target), optional `output_path`. Returns `{"record_id": "..."}`.
3. (Poll) `scope.record_status` with `record_id` — returns `{"done": bool, "elapsed_s": float, "sample_count": int}`. Poll at a reasonable interval (every few seconds for long captures).
4. `scope.record_stop` with `record_id` — stop early or after `done=true` — returns the final artifact path and sample count.

**Tools + sequence (logic record):**

1. `logic.configure` — `pins` list (e.g. `["dio0", "dio1", "dio2", "dio3"]`), `sample_rate_hz`, `buffer_size`.
2. `logic.record_start` — params: `duration_s`, optional `output_path`. Returns `{"record_id": "..."}`.
3. `logic.record_status` — poll until `done`.
4. `logic.record_stop` — finalize and return artifact path.

After a logic record, run `decoder.i2c`, `decoder.spi`, `decoder.uart`, or `decoder.can` on the saved NPZ to decode protocol transactions from the raw digital trace.

**Formulae:** None.

**Interpretation:**

- The NPZ from a scope record has `time_s` and per-channel `voltage` arrays, same schema as a buffered capture.
- The NPZ from a logic record has `time_s` and per-pin `data` arrays (uint8, 0/1 per sample).
- `lost_samples > 0` in the record-stop result means the record buffer overflowed — the host could not drain samples fast enough. Reduce `sample_rate_hz` or `duration_s`.

**Gotchas:**

- In a VM (QEMU/Proxmox), emulated-USB latency spikes can occasionally overflow streaming buffers. A one-off `lost_samples` failure in a hardware test is more likely flake than regression — rerun before debugging.
- The 32 MB raw-sample memory cap for async sniff sessions does not apply to scope/logic record; record mode is bounded by `duration_s` and available disk space.

---
id: time-domain:logic-timing
tools: [waveforms.open, waveforms.list_pins, logic.configure, logic.set_trigger, logic.capture]
---

## Logic Timing Capture (Single Buffer)

**Goal / when to use:** Capture a short deterministic digital event — a SPI transaction, a GPIO pulse, a bus cycle — with precise timing. Triggers on a pin edge. Use for timing measurements, glitch detection, or a quick logic trace before deciding whether to run a protocol decoder.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm DIO pins are free.

**Wiring:** Connect DIO pins to the digital signals. No analog connection needed.

**Tools + sequence:**

1. `logic.configure` — `pins` list, `sample_rate_hz`, `buffer_size`.
2. `logic.set_trigger` — `source` (`"none"`, `"detector"`, or `"external"`), `pin` (the trigger pin), `edge` (`"rising"`, `"falling"`, or `"either"`), `position` (pre-trigger fraction, 0.0–1.0), `timeout_s`. Setting `source: "none"` is a free-run capture.
3. `logic.capture` — fires the capture and returns an NPZ artifact + summary.

Then optionally:

4. `decoder.i2c` / `decoder.spi` / `decoder.uart` / `decoder.can` — decode protocol transactions from the captured NPZ. Each takes the `capture_path` and pin assignments (see protocol recipes).

**Formulae:** Timing: read `time_s` array from the NPZ; edge timestamps computed from transitions in the `data` array.

**Interpretation:** The NPZ `data` array has shape `(n_samples, n_pins)`, dtype uint8 (0/1 per pin). The `time_s` array is the corresponding time axis. Transitions in `data[:, pin_index]` give edge timestamps.

**Gotchas:** Triggering with `source: "detector"` fires on the first qualifying edge on `pin`; with `source: "external"` it fires on the AD3's external trigger input. `source: "none"` captures immediately without waiting.

---
id: time-domain:pattern-gen
tools: [waveforms.open, waveforms.list_pins, pattern.configure, pattern.start, pattern.stop]
---

## Pattern Generator

**Goal / when to use:** Drive a repetitive digital pattern (clock, pulse, random, or custom) on a DIO pin. Use to generate a clock for a DUT, produce a PWM signal, or drive a test stimulus without tying up the AWG.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm the target DIO pin is free.

**Wiring:** Connect the DIO pin to the DUT's clock, enable, or data input. Ensure I/O voltage compatibility (3.3 V default).

**Tools + sequence:**

1. `pattern.configure` — `pin` (e.g. `"dio0"`), `function` (`"Pulse"`, `"Clock"`, `"Random"`, `"Custom"`), `frequency_hz`, `duty_cycle` (0.0–1.0, for Pulse/Clock), `idle_state` (0 or 1, the level when not running). For `"Custom"`, supply a `data` byte array encoding the bit pattern.
2. `pattern.start` — arms and drives the pattern continuously on the pin.
3. `pattern.stop` — stops the pattern and returns the pin to `idle_state`.

**Formulae:** For `"Clock"`: period = `1/frequency_hz`, high time = `duty_cycle/frequency_hz`.

**Interpretation:** The pattern runs indefinitely until `pattern.stop`. `"Random"` generates a pseudorandom bit sequence at the configured rate — useful for bit-error-rate tests or to keep a bus active during a sniff session.

**Gotchas:** `pattern.start` routes through `device.gate_output` (logged, safety-gated). The pin must not be claimed by another instrument (i.e. not also configured as a DIO output or a protocol master pin). `waveforms.list_pins` shows current owners.
