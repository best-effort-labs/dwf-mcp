# Bench Reference — wiring · ground topology · capability gating · pin allocation · gotchas

Cross-cutting facts that every recipe depends on. Read this before writing your first tool call.

---
id: bench:session-setup
tools: [waveforms.open, waveforms.status, waveforms.list_pins]
---

## Session Setup

**Goal / when to use:** Establish a device session and discover the physical pin inventory before any measurement.

**Preflight:** None — `waveforms.open` is the first call in every session.

**Wiring:** None at this stage.

**Tools + sequence:**

1. `waveforms.open` — enumerate and open the device; optionally pass `device_serial` to pick a specific unit, `idle_timeout_s` to tune the auto-close window, and safety-policy kwargs (`supply_max_voltage_pos`, `awg_max_amplitude_v`, etc.) to bound what can be driven. Returns the device serial, model, firmware version, and the hardware capability caps (buffer max, sample-rate max). Those caps feed `spectrum.configure` / `bode.configure` buffer sizing.
2. `waveforms.list_pins` — list every physical pin (DIO0…DIO15, W1/W2 AWG outputs, CH1/CH2 scope inputs, VPos/VNeg supply rails), the current allocator owner for each, and the device-level resource groups. Use this to discover valid pin identifiers, check for conflicts before a multi-instrument setup, and understand what is already claimed.
3. `waveforms.status` — query open/closed state and idle-timeout countdown without touching hardware. Useful to confirm the device is still open before a long tool sequence.

**Formulae:** None.

**Interpretation:** `waveforms.open` returns `buffer_max` and `sample_rate_max_hz` — these are the hardware ceilings. `spectrum.configure` and `bode.configure` validate `buffer_size` and `sample_rate_hz` against them; exceeding either raises `PinAllocationError` or `InstrumentNotConfigured`.

**Gotchas:**

- Re-calling `waveforms.open` on an already-open device is idempotent — it returns the current session without reopening hardware. To force a clean restart, call `waveforms.close` first.
- After an idle timeout the device handle is released; the next tool call raises `DwfDeviceLost`. Recover by calling `waveforms.open` again — instrument state is cleared, so reconfigure before measuring.
- On the Digital Discovery (`devid 4`), `waveforms.list_pins` shows only digital pins — there are no analog-in/out entries. The analog instruments (`scope`, `awg`, `spectrum`, `bode`, `impedance`) are capability-gated out and will raise `InstrumentNotConfigured` if called.

---

## Ground Topology

The single most common wiring mistake on the Analog Discovery 3 (and other Digilent analog devices) is a floating differential input.

**AD3 analog inputs are true differential — CH1_P/CH1_N and CH2_P/CH2_N both float independently.** If `CH_N` is left unconnected, both `CH_P` and `CH_N` are floating, and the scope reads random garbage.

**Rule: always bridge CH_N to the circuit ground** before taking any analog measurement.

- Connect `CH1_NEG` (scope CH1 minus input) to the signal-ground node.
- Connect `CH2_NEG` (scope CH2 minus input) to the signal-ground node.
- Connect `GND` (the AD3's ground pin) to the circuit ground as well; this is the return path for AWG and supply currents.

For Jumperless-routed signals, bridge `AD3_GND` to the Jumperless GND rail. The Jumperless routes signals but has no implicit ground-plane; the AD3 must share a ground explicitly.

The differential topology is a feature (rejects common-mode noise), but it requires the user to close the return path.

---

## Capability Gating

Not every instrument is available on every device. The server gates instruments at the device-profile level — if the profile does not include an instrument, calling it raises `InstrumentNotConfigured`.

| Device | Analog-in/out | Digital I/O | Notes |
|--------|--------------|-------------|-------|
| AD1 / AD2 / AD3 | scope, awg, supply, dmm, spectrum, bode, impedance | logic, pattern, dio, protocol masters+sniff | Full mixed-signal |
| ADP2230 (`devid 14`) | scope (CH1/CH2), awg (W1 only — W2/W3 are internal), supply, dmm, spectrum, bode, impedance | logic, pattern, dio (with bank-global pull, see below) | 1 user AWG output |
| Digital Discovery (`devid 4`) | **none** | logic, pattern, dio, protocol masters+sniff | Digital-only — analog instruments raise `InstrumentNotConfigured` |

Instruments that require both an AWG and two analog-in channels (`bode`, `impedance`) will also raise `InstrumentNotConfigured` if the device profile does not have at least one user AWG channel and two analog-in channels.

The `waveforms.open` return value includes `devid` and `capabilities`; check these if you are unsure whether an instrument is available on the connected device.

---

## Pin Allocation

The server uses a `PinAllocator` to enforce mutual exclusion on physical resources. Each instrument `claim`s the pins it needs before touching hardware and `release`s them in a `finally` block.

Key rules:

- A pin can only be owned by one instrument at a time (except `claim_observe`, which lets a sniffer coexist with an active master on the same wires — see the sniff recipes).
- The frequency-domain instruments (`spectrum`, `bode`, `impedance`) claim analog-in channels under their own name, *not* under `"scope"`. This means a live `spectrum` blocks a concurrent `scope.capture` on the same channel, but the correct error (`PinAllocationError`) fires — it does not silently corrupt.
- `bode` and `impedance` claim both the AWG pin and all analog-in pins for the whole sweep. Do not interleave other AWG or scope calls during a `bode.measure` or `impedance.measure`.

Use `waveforms.list_pins` to inspect the current claim state. Releasing happens automatically when an instrument finishes a measurement or on `waveforms.close`.

---

## Hardware Gotchas

These gotchas recur across multiple recipes. Each domain file cross-references them by name.

### Stale-first-AnalogIn-buffer

The first AnalogIn acquisition after a device open returns a buffer from a prior internal capture, reading approximately 19 dB lower than the true signal. This affects any path that arms AnalogIn without a prior triggered acquisition. **Affected paths:** `spectrum.measure`, `bode.measure`, `impedance.measure`, and free-run `scope.capture` with `source: "none"` (no trigger).

Mitigation: the server discards one warm-up acquisition automatically in `spectrum.measure`, `bode.measure`, and `impedance.measure`. For triggered `scope.capture`, the trigger-wait masks the stale buffer. For free-run `scope.capture` (trigger `source: "none"`), if you see a suspiciously low first reading, call `scope.capture` a second time.

### Settle-before-arm

`scope_arm()` **starts** a single AnalogIn acquisition — it does not just arm a future trigger. Any stimulus (AWG retune, RC settling, signal routing) must complete *before* calling `scope.capture` or `bode.measure` / `impedance.measure`. Settling after the arm call is too late.

In practice: configure the AWG and let it settle, then capture. The sweep instruments (`bode`, `impedance`) honor this internally — the settle delay runs before arm on every point.

### Jumperless RC Settling (~125 ms)

Jumperless-routed analog signals pass through the breadboard's routing fabric. The AD3 scope inputs have non-negligible input capacitance, and the Jumperless routing adds resistance. The resulting RC time constant produces a ~125 ms settling tail on step changes. Use ≥ 0.5 s between signal routing changes and the next scope capture when measuring through the Jumperless.

### ADP2230 Bank-Global DIO Pull

On the ADP2230, `dio.set_pull` applies to the **entire DIO bank**, not the individual pin. The SDK expands a single-pin pull configuration to all 16 pins in the bank (0xFFFF). If you need only one pin to have a pull, set all other pins explicitly with `dio.set_pull` calls, or note the bank-wide change in your procedure.

### Protocol.SPI / DigitalIn Coexistence

The AD3 SPI protocol engine uses DigitalIn internally. Therefore `spi.configure` (active master) cannot coexist with a `sniff.spi_start` (observe-mode DigitalIn) on the same device at the same time. For SPI, use either the active master *or* the sniffer — not both simultaneously.

I2C, UART, and CAN do not have this constraint: their sniff paths use separate engine blocks and can coexist with active masters (see `sniff.i2c_start` concurrency example in the protocols recipes).

### TOP_RAIL and RP2350B GPIO Voltage Tolerance

When using a Jumperless V5 with the RP2350B as a protocol stimulus source:

- Set the Jumperless TOP_RAIL to **3.3 V** for any GPIO routed through pull-ups to the RP2350B.
- RP2350B GP20 and GP21 (I2C hardware pins) are **not 5V tolerant** — applying 5V will damage the chip.
- For UART stimulus on the RP2350B, use bare `machine.UART(0, baud)` (no Pin override). SoftI2C hangs; use hardware `machine.I2C(0, scl=Pin(21), sda=Pin(20), freq=10000)` instead.
