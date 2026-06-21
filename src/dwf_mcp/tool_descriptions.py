"""One-line `what / when` description per MCP tool. Sourced into list_tools and the
cookbook index. Kept exhaustive by tests/unit/test_tool_descriptions.py."""
from __future__ import annotations

TOOL_DESCRIPTIONS: dict[str, str] = {
    # --- waveforms meta tools ---
    "waveforms.open": (
        "Open (connect to) a WaveForms device; must be called first — returns serial, model, "
        "firmware, and buffer/rate caps."
    ),
    "waveforms.close": (
        "Close the currently-open device and release all hardware resources; call before "
        "swapping devices or at session end."
    ),
    "waveforms.status": (
        "Return current device status (open/closed, serial, model, idle timeout) without "
        "affecting hardware state."
    ),
    "waveforms.list_pins": (
        "List the open device's physical pins, current allocator claims, resource groups, "
        "and limits; use to discover valid pin identifiers and check for conflicts."
    ),

    # --- AWG (analog waveform generator) ---
    "awg.configure": (
        "Configure an AWG channel with a standard waveform (Sine, Square, Triangle, DC, "
        "Noise, etc.) — call before awg.start."
    ),
    "awg.upload_custom": (
        "Upload a custom waveform from an NPY file of normalised [-1, 1] samples to an AWG "
        "channel — use for arbitrary signals."
    ),
    "awg.start": (
        "Start (arm) a previously configured AWG channel to begin generating its waveform "
        "on the hardware output pin."
    ),
    "awg.stop": (
        "Stop an active AWG channel and silence its output; the channel configuration is "
        "preserved so start() can restart it."
    ),

    # --- Bode / network analyzer ---
    "bode.configure": (
        "Configure a Bode sweep: frequency range, point count, drive channel, reference "
        "and DUT scope channels, and amplitude — call before bode.measure."
    ),
    "bode.measure": (
        "Run the configured Bode sweep (AWG sine + ratiometric scope capture at each "
        "frequency) and return gain/phase vs. frequency as an NPZ artifact."
    ),

    # --- CAN bus ---
    "can.configure": (
        "Configure the CAN protocol engine on two DIO pins with a given bit rate — "
        "must be called before can.send or can.receive."
    ),
    "can.send": (
        "Transmit a single CAN frame (standard or extended ID, up to 8 data bytes) on "
        "the configured CAN bus."
    ),
    "can.receive": (
        "Block until one CAN frame arrives on the configured bus (or timeout) and return "
        "its ID, data, and error count."
    ),

    # --- Decoder (post-process logic captures) ---
    "decoder.can": (
        "Decode a CAN bus from an existing logic capture NPZ artifact and write a Parquet "
        "file of frames with IDs and data."
    ),
    "decoder.i2c": (
        "Decode I2C transactions from an existing logic capture NPZ artifact and write a "
        "Parquet file of address/data records."
    ),
    "decoder.spi": (
        "Decode SPI transactions from an existing logic capture NPZ artifact and write a "
        "Parquet file of transferred bytes."
    ),
    "decoder.uart": (
        "Decode UART frames from an existing logic capture NPZ artifact and write a "
        "Parquet file of received bytes and framing errors."
    ),

    # --- DIO (digital I/O) ---
    "dio.read": (
        "Read the current logic level of a single DIO or DIN pin; the pin direction must "
        "be 'in' (or unset)."
    ),
    "dio.set": (
        "Drive a DIO pin to a logic high or low; requires set_direction('out') to have "
        "been called first."
    ),
    "dio.set_direction": (
        "Set a DIO pin's direction to 'in' (high-impedance input) or 'out' (driven "
        "output); call before dio.set or dio.read."
    ),
    "dio.set_drive": (
        "Set the output drive strength (mA) and slew rate for a DIO bank — use on "
        "devices that expose per-bank drive control (e.g. ADP2230)."
    ),
    "dio.set_pull": (
        "Configure the pull resistor mode (up / down / none / keeper) for a DIO pin; "
        "on bank-global devices (ADP2230) this applies to the whole bank."
    ),
    "dio.set_voltage": (
        "Set the DIO logic voltage level (e.g. 1.8 V, 3.3 V) for the digital I/O bank "
        "on devices that support adjustable I/O voltage."
    ),

    # --- DMM ---
    "dmm.measure": (
        "Take a high-accuracy averaged DC or AC voltage measurement on an analog input "
        "channel; returns mean, min, max, and RMS values."
    ),

    # --- I2C active master ---
    "i2c.configure": (
        "Configure the I2C master engine on two DIO pins (SDA + SCL) with clock rate and "
        "optional software pull-ups — call before any i2c read/write."
    ),
    "i2c.read": (
        "Read N bytes from an I2C peripheral at the given 7-bit address using the "
        "configured master engine."
    ),
    "i2c.scan": (
        "Scan all 7-bit I2C addresses and return responsive devices (requires i2c.configure "
        "first); use to discover peripherals on the bus."
    ),
    "i2c.write": (
        "Write a byte array to an I2C peripheral at the given 7-bit address using the "
        "configured master engine."
    ),
    "i2c.write_read": (
        "Perform a combined I2C write-then-read (register-read pattern) in a single bus "
        "transaction — typical for reading sensor registers."
    ),

    # --- Impedance analyzer ---
    "impedance.configure": (
        "Configure an impedance sweep: frequency range, point count, series reference "
        "resistor value, drive channel, and scope channels — call before impedance.measure."
    ),
    "impedance.measure": (
        "Run the configured impedance sweep (W1 -> R_ref -> DUT -> GND, ratiometric CH1/CH2) "
        "and return |Z|, phase, R, X, C, L, Q, D vs. frequency as an NPZ artifact."
    ),

    # --- Logic analyzer ---
    "logic.capture": (
        "Trigger (or free-run) a single buffer-mode logic capture on the configured pins "
        "and return an NPZ/VCD artifact; use for short deterministic captures."
    ),
    "logic.configure": (
        "Configure the logic analyzer: pin list, sample rate, and buffer size — "
        "call before logic.set_trigger and logic.capture."
    ),
    "logic.record_start": (
        "Start a long streaming logic record to an NPZ/VCD artifact; returns a record_id — "
        "use for captures longer than the hardware buffer."
    ),
    "logic.record_status": (
        "Poll the status of a running logic record by record_id; returns elapsed time, "
        "sample count, and whether it has finished."
    ),
    "logic.record_stop": (
        "Stop a running logic record early and return the final artifact path and sample "
        "count; also cleans up completed sessions."
    ),
    "logic.set_trigger": (
        "Set the logic analyzer trigger: source (none / detector / external), pin, edge "
        "condition, pre-trigger position, and timeout."
    ),

    # --- Pattern generator ---
    "pattern.configure": (
        "Configure a digital pattern on one DIO pin: function (Pulse, Clock, Random, "
        "Custom), frequency, duty cycle, and idle state — call before pattern.start."
    ),
    "pattern.start": (
        "Arm and start the configured pattern generator on a pin, driving the digital "
        "output continuously until pattern.stop is called."
    ),
    "pattern.stop": (
        "Stop the pattern generator on a pin and return the pin to its idle state; "
        "the configuration is preserved for a future start."
    ),

    # --- Scope (analog in) ---
    "scope.capture": (
        "Trigger (or free-run) a single buffer-mode scope capture on the configured "
        "channels and return an NPZ artifact; use for time-domain waveforms."
    ),
    "scope.configure": (
        "Configure scope channels: voltage range, offset, coupling (DC/AC), sample rate, "
        "and buffer size — call before scope.set_trigger and scope.capture."
    ),
    "scope.record_start": (
        "Start a long streaming analog record to NPZ; returns a record_id — use for "
        "captures longer than the hardware buffer (record mode)."
    ),
    "scope.record_status": (
        "Poll the status of a running scope record by record_id; returns elapsed time, "
        "sample count, and whether it has finished."
    ),
    "scope.record_stop": (
        "Stop a running scope record early and return the final artifact path and sample "
        "count; also cleans up completed sessions."
    ),
    "scope.set_trigger": (
        "Set the scope trigger: source (none / analog / external), channel, threshold "
        "voltage, edge condition, pre-trigger position, and timeout."
    ),

    # --- Sniff: one-shot blocking passive protocol capture ---
    "sniff.can": (
        "Passively capture CAN frames for a fixed duration (blocking) and return decoded "
        "frames plus a Parquet artifact; use for short known-duration sniffs."
    ),
    "sniff.i2c": (
        "Passively capture I2C transactions for a fixed duration (blocking) and return "
        "decoded records plus a Parquet artifact; use for short known-duration sniffs."
    ),
    "sniff.uart": (
        "Passively capture UART bytes for a fixed duration (blocking) and return decoded "
        "frames plus a Parquet artifact; use for short known-duration sniffs."
    ),

    # --- Sniff: async start/status/stop for SPI ---
    "sniff.spi_start": (
        "Start a non-blocking background SPI sniff session; returns a sniff_id — use "
        "when you need to poll status or stop early."
    ),
    "sniff.spi_status": (
        "Poll an active SPI sniff session by sniff_id; reports samples received, lost "
        "samples, and whether the session is done (decoded frames come from sniff.spi_stop)."
    ),
    "sniff.spi_stop": (
        "Stop a running SPI sniff session and return the final decoded frames plus "
        "artifact path; also releases hardware resources."
    ),

    # --- Sniff: async start/status/stop for I2C ---
    "sniff.i2c_start": (
        "Start a non-blocking background I2C sniff session; returns a sniff_id — use "
        "when you need to poll status or stop early."
    ),
    "sniff.i2c_status": (
        "Poll an active I2C sniff session by sniff_id; reports samples received, lost "
        "samples, and whether the session is done (decoded transactions come from sniff.i2c_stop)."
    ),
    "sniff.i2c_stop": (
        "Stop a running I2C sniff session and return the final decoded transactions plus "
        "artifact path; also releases hardware resources."
    ),

    # --- Sniff: async start/status/stop for UART ---
    "sniff.uart_start": (
        "Start a non-blocking background UART sniff session; returns a sniff_id — use "
        "when you need to poll status or stop early."
    ),
    "sniff.uart_status": (
        "Poll an active UART sniff session by sniff_id; reports samples received, lost "
        "samples, and whether the session is done (decoded frames come from sniff.uart_stop)."
    ),
    "sniff.uart_stop": (
        "Stop a running UART sniff session and return the final decoded frames plus "
        "artifact path; also releases hardware resources."
    ),

    # --- Sniff: async start/status/stop for CAN ---
    "sniff.can_start": (
        "Start a non-blocking background CAN sniff session; returns a sniff_id — use "
        "when you need to poll status or stop early."
    ),
    "sniff.can_status": (
        "Poll an active CAN sniff session by sniff_id; reports samples received, lost "
        "samples, and whether the session is done (decoded frames come from sniff.can_stop)."
    ),
    "sniff.can_stop": (
        "Stop a running CAN sniff session and return the final decoded frames plus "
        "artifact path; also releases hardware resources."
    ),

    # --- Spectrum analyzer ---
    "spectrum.configure": (
        "Configure the spectrum analyzer: channel, sample rate, buffer size, window "
        "function, averaging count, and amplitude mode — call before spectrum.measure."
    ),
    "spectrum.measure": (
        "Acquire a spectrum capture using the configured settings and return frequency "
        "bins and magnitudes as an NPZ artifact."
    ),
    "spectrum.transform": (
        "Compute an FFT spectrum from an existing scope capture NPZ artifact without "
        "re-acquiring; use to re-analyze a prior waveform with a different window function."
    ),

    # --- SPI active master ---
    "spi.configure": (
        "Configure the SPI master engine: clock pin, frequency, mode (0-3), optional "
        "MOSI/MISO/CS pins — call before any spi transfer."
    ),
    "spi.read": (
        "Read N bytes from the SPI bus (MISO), driving MOSI low during the transfer; "
        "CS is asserted/deasserted automatically."
    ),
    "spi.transfer": (
        "Perform a full-duplex SPI transfer: write data bytes on MOSI and capture the "
        "corresponding MISO bytes simultaneously."
    ),
    "spi.write": (
        "Write bytes to the SPI bus (MOSI) without capturing MISO; CS is "
        "asserted/deasserted automatically."
    ),

    # --- Supply (programmable power rails) ---
    "supply.disable": (
        "Disable (de-energise) a supply rail (vpos or vneg) to remove power from "
        "the DUT; leaves the voltage setpoint staged for re-enable."
    ),
    "supply.enable": (
        "Enable (energise) a supply rail at its staged setpoint after passing the "
        "safety gate; use after supply.set to power the DUT."
    ),
    "supply.read": (
        "Read back the live voltage and current drawn on a supply rail; use to "
        "verify the DUT draw after enabling."
    ),
    "supply.set": (
        "Stage a voltage (and optional current limit) for a supply rail; call before "
        "supply.enable, or while energised to change the live setpoint."
    ),

    # --- UART active master ---
    "uart.configure": (
        "Configure the UART engine: baud rate, optional TX/RX pins, data bits, parity, "
        "and stop bits — call before uart.read or uart.write."
    ),
    "uart.read": (
        "Read up to N bytes from the UART RX pin with a configurable timeout; returns "
        "the received bytes as a list."
    ),
    "uart.write": (
        "Write a byte array to the UART TX pin using the configured baud rate and "
        "framing parameters."
    ),
}
