# Troubleshooting & known limitations

Practical notes for when something doesn't behave as expected. For the system
design behind these behaviors, see [architecture.md](architecture.md).

## Device loss / unplug

**Behavior:** If the AD3 is physically unplugged (or USB drops) mid-session, the
**next tool call that actually touches the device** raises a backend error.
Currently that surfaces as a raw `DwfLibraryError` ("communication failed")
rather than a clean `DwfDeviceLost`, and the allocator/instrument state is left
as-is until you recover.

**Recovery:** call `waveforms.close` then `waveforms.open` (or restart the
server). Close clears the allocator and all instrument state.

**Why it isn't detected automatically.** There is no cheap, side-effect-free way
to detect a *physical* unplug while a handle is held open. Two obvious probes
were tried and **both fail** (empirically verified against an AD3, June 2026):

- A device-parameter read (`paramGet`) is served from host-side memory and keeps
  returning a value long after the device is gone.
- Re-enumerating the USB bus keeps listing a device we still hold open, even
  after it has been physically pulled.

Only a genuine live I/O transaction fails on disconnect, and using one as a
heartbeat on every call risks disturbing in-flight captures. So `is_open`
deliberately reflects "do we hold a handle" rather than "is the device live,"
and a real unplug surfaces on the next operation as above. **Do not re-add a
passive `is_open` probe expecting it to catch unplug — it won't.**

What *is* handled cleanly (these return a proper `DwfDeviceLost`): never-opened
device, explicitly-closed device, and idle-timeout-closed device.

## Hardware setup

### Digilent runtime

`pydwf` needs the Digilent WaveForms runtime (`libdwf`) installed; on macOS it is
located via Digilent's framework, on Linux via `libdwf.so`. See the README
[Install](../README.md#install) section for the two Digilent packages required
and the no-login download links.

### Multiple devices: selecting the right unit

With more than one Analog Discovery on the bus, `waveforms.open` with no
`device_serial` opens **enum index 0** — *not necessarily* the unit your signals
are wired to. Pass `device_serial` (the hardware test suite reads
`DWF_TEST_SERIAL`) to pin the DUT.

Symptom of getting this wrong: capture/record tools return **cleanly-clocked but
all-zero / empty** data (acquisition reaches `Triggered`, streams the right
sample count with `lost=0`, but every value is ~0) — that's an *unwired, floating
input* on the wrong device, not a hardware fault. Buffer/single-shot reads of a
known signal confirm whether you're talking to the wired unit. (This exact trap
cost a long debugging detour once the bench harness moved between units.)

### Linux: no `/dev/ttyACM*` / device not found

Minimal and cloud kernel flavors (e.g. Ubuntu's `virtual` cloud image) omit USB
serial drivers, so the AD3 enumerates on the bus but no device nodes appear and
nothing explains why in `dmesg`. Fix:

```bash
sudo apt install linux-modules-extra-$(uname -r)
```

This is the most common "works on my laptop, not in the VM" cause.

### VMs (QEMU / KVM / Proxmox)

USB passthrough must attach the AD3 (VID:PID `1443:7003`) directly to the guest;
chained hubs are unreliable. USB config changes apply only on a full VM
stop/start, not a guest-initiated reboot. Pair with `linux-modules-extra` above.

## Digital Discovery

- **DIN pins are input-only.** Calling `dio.set` or `pattern` on a `dinN` pin, or
  asking `dio.set_direction(..., 'out')`, raises `ValueError`. Use the
  bidirectional `dio24..dio39` pins for outputs.
- **DIN pull is bank-global.** `dio.set_pull("dinN", ...)` affects **all 24 DIN
  pins** (DINPP register), not just one. This is an SDK limitation, not a
  per-pin override.
- **Adjustable DIO voltage.** Unlike classic Analog Discovery (fixed 3.3 V),
  Digital Discovery supports `dio.set_voltage(1.2...3.3)` for all
  `dio24..dio39` pins.
- **Selecting when multiple devices are attached.** With more than one DD or a
  mix of devices, pass `device_serial` to `waveforms.open` (the hardware test
  suite reads `DWF_TEST_SERIAL`). For example, this lab's DD unit is serial
  `210321AD4ECF`. See the note under [Multiple devices](#multiple-devices-selecting-the-right-unit).

## Safety policy

- The `SafetyPolicy` is **latched at `waveforms.open` and immutable** for the
  session. To change a limit, `waveforms.close` then `waveforms.open` with new
  values — there is intentionally no mid-session override.
- A rejected output raises `SafetyViolation` naming the requested vs. allowed
  value, and is recorded (along with every accepted output-enable) in
  `<workspace>/dwf-safety.log`.
- Outputs never auto-energize: `supply.set` stages a setpoint; `supply.enable`
  energizes. If you change the setpoint of an *already-enabled* rail (or
  reconfigure a *running* AWG channel), that path is gated too.

## Sniff / capture memory limit

`sniff.*_start` rejects a capture projected to exceed **32 MB** of raw storage.
Record mode stores the full 16-bit digital bank — **16 bytes per sample**,
regardless of how many pins your protocol uses — so the limit is reached sooner
than a per-pin estimate suggests. The error message suggests a smaller
`max_duration_s`. For long captures, lower `sample_rate_hz`, shorten the
duration, or use `stream_decode: true` so chunks are decoded and discarded
instead of accumulated.

## Common errors

| Error `type` | Meaning | Fix |
|--------------|---------|-----|
| `DwfDeviceLost` | Device not open (closed, idle-expired, never opened). | `waveforms.open`. |
| `PinAllocationError` | A pin/engine is already claimed by another instrument. | Release the other instrument, or pick different pins. `waveforms.list_pins` shows current claims. |
| `SafetyViolation` | Requested voltage/amplitude exceeds the latched policy. | Reopen with a higher cap, or lower the request. |
| `InstrumentNotConfigured` | A tool was called before its `configure`. | Call `<instrument>.configure` first. |
| raw `DwfLibraryError` | Usually a physical unplug / comms failure mid-call. | See [Device loss](#device-loss--unplug); close + reopen. |

## Persisted artifacts vs. tool results

Tool results return **paths plus a summary, not raw samples**. The actual data
lives under `<workspace>/captures/` as `.npz` (raw), `.parquet` (decoded), and
optional `.vcd`, each with a `.json` sidecar capturing the full config and the
safety-policy snapshot at capture time. If a result looks empty, check the
referenced file — the payload is on disk by design.
