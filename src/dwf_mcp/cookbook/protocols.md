# Protocol Recipes — i2c · spi · uart · can — configure / transfer / sniff / decode

Active master and passive sniffer recipes for the four built-in protocols. Active masters (I2C, SPI, UART, CAN configure/transfer) use the device's protocol engine. Passive sniffers use either engine-mode (blocking one-shot) or observe-mode (async, coexists with active masters).

See `dwf://cookbook/bench` for ground topology, pin-voltage rules, and the SPI/DigitalIn coexistence gotcha.

---
id: protocols:i2c-master
tools: [waveforms.open, waveforms.list_pins, i2c.configure, i2c.scan, i2c.write, i2c.read, i2c.write_read]
---

## I2C Active Master — Scan, Read, Write

**Goal / when to use:** Drive the AD3 as an I2C master to discover, read from, or write to I2C peripherals on a bus. Use to configure sensors, read registers, or exercise a firmware I2C driver from the bench.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm `dio_sda` and `dio_scl` pins are free. The I2C engine claims `i2c_engine` as a virtual resource — only one I2C master can be active at a time.

**Wiring:**

- `SDAPIN` (configurable DIO, e.g. `"dio0"`) → SDA line (with pull-up to VCC).
- `SCLPIN` (configurable DIO, e.g. `"dio1"`) → SCL line (with pull-up to VCC).
- `GND` → bus GND.
- Pull-ups: the `i2c.configure` `pullup` parameter can enable the AD3's built-in software pull-ups (~4.7 kΩ). For higher bus speeds or long traces, use external pull-ups and disable the internal ones.

**Jumperless V5 stimulus (RP2350B):** use hardware `machine.I2C(0, scl=Pin(21), sda=Pin(20), freq=10000)` — SoftI2C hangs. TOP_RAIL must be 3.3 V; GP20/GP21 are not 5V tolerant.

**Tools + sequence:**

1. `i2c.configure` — `sda_pin`, `scl_pin`, `clock_hz` (e.g. 100000 for standard, 400000 for fast), `pullup` (true/false). Must be called before any I2C read/write/scan.
2. `i2c.scan` — scans all 7-bit addresses (0x08–0x77) and returns a list of responsive device addresses. Use to discover what is on the bus before reading.
3. `i2c.write` — `address` (7-bit), `data` (byte list). Writes bytes to the peripheral.
4. `i2c.read` — `address`, `n_bytes`. Reads N bytes from the peripheral.
5. `i2c.write_read` — `address`, `write_data`, `n_read_bytes`. Combined write-then-read in a single bus transaction — the standard register-read pattern for most sensors (write register address, read register value).

**Formulae:** None.

**Interpretation:**

- `i2c.scan` returns `[address, ...]` — an empty list means no devices responded (check wiring and pull-ups).
- `i2c.write` and `i2c.read` return the byte count and acknowledgment status.
- `i2c.write_read` is the most common pattern: write 1–2 register-address bytes, read back N data bytes.

**Gotchas:**

- `i2c.scan` requires `i2c.configure` first — calling it without configuration raises `InstrumentNotConfigured`.
- I2C lines must be pulled high (either external pull-ups or `pullup=true`). Without pull-ups, SCL and SDA float and every transaction fails with a timeout.
- The I2C engine claims `i2c_engine`; a concurrent `sniff.i2c` (engine-mode) would also claim `i2c_engine` and conflict. Use `sniff.i2c_start` (observe-mode DigitalIn) for concurrent sniffing — see the sniff recipe below.

---
id: protocols:i2c-sniff
tools: [waveforms.open, waveforms.list_pins, sniff.i2c, sniff.i2c_start, sniff.i2c_status, sniff.i2c_stop, decoder.i2c]
---

## I2C Passive Sniff

**Goal / when to use:** Observe I2C transactions on a live bus without disturbing it. Two modes: blocking one-shot (`sniff.i2c` for short known-duration captures) and async observe-mode (`sniff.i2c_start/status/stop` for longer or concurrent captures).

The **async observe-mode** (`sniff.i2c_start`) uses the DigitalIn engine rather than the protocol engine — it can therefore coexist with a concurrent `i2c.configure` active master on the same wires (see README example 2). The blocking mode (`sniff.i2c`) uses the protocol engine and cannot run concurrently.

**Preflight:** `waveforms.open`; SDA and SCL pins connected to the bus.

**Wiring:** Connect SDA and SCL pins (DIO) to the bus. No pull-ups needed from the AD3 — the bus already has pull-ups from the device under test.

**Tools + sequence — blocking one-shot:**

1. `sniff.i2c` — `sda_pin`, `scl_pin`, `clock_hz`, `duration_s`. Blocks for `duration_s`, then returns decoded transactions + Parquet artifact path.

**Tools + sequence — async observe-mode (with concurrent active master):**

1. `sniff.i2c_start` — `sda_pin`, `scl_pin`, `clock_hz`, `max_duration_s`, optional `stream_decode` (true to decode live in chunks, removing the 32 MB raw-sample cap). Returns `{"sniff_id": "..."}`.
2. (Meanwhile, optionally) `i2c.configure` + `i2c.scan` / `i2c.write` / `i2c.read` — these use the protocol engine and do not conflict with the DigitalIn-based sniff.
3. `sniff.i2c_status` — poll with `sniff_id`; returns `{"done": bool, "samples_received": int, "lost_samples": int}`.
4. `sniff.i2c_stop` — stop and return decoded transactions + Parquet artifact. (Or let `max_duration_s` expire; `sniff.i2c_status` reports `done=true` when it finishes.)

**Post-process decode (from a logic record):**

5. `decoder.i2c` — `capture_path` (logic NPZ), `sda_pin`, `scl_pin`. Decodes I2C transactions from a previously recorded raw logic NPZ and writes a Parquet file of address/data records.

**Formulae:** None.

**Interpretation:** The decoded output (from `sniff.i2c`, `sniff.i2c_stop`, or `decoder.i2c`) is a Parquet file with columns for address, direction (read/write), data bytes, and NACK/error flags. The `summary` in the stop result includes transaction count and any error counts.

**Gotchas:**

- `sniff.i2c` (engine-mode, blocking) cannot run concurrently with `i2c.configure` (both claim `i2c_engine`). Use `sniff.i2c_start` for concurrent capture.
- `stream_decode: true` bypasses the 32 MB raw-sample cap — use for captures longer than a few seconds at typical I2C rates.
- `lost_samples > 0` means the record buffer overflowed. Reduce `max_duration_s` or `sample_rate_hz`, or use `stream_decode: true`.

---
id: protocols:spi-master
tools: [waveforms.open, waveforms.list_pins, spi.configure, spi.transfer, spi.write, spi.read]
---

## SPI Active Master — Transfer, Write, Read

**Goal / when to use:** Drive the AD3 as an SPI master to communicate with SPI peripherals. Full-duplex `spi.transfer` for simultaneous MOSI/MISO; `spi.write` for write-only (display, DAC); `spi.read` for read-only.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm `clk_pin`, `mosi_pin`, `miso_pin`, `cs_pin` DIO pins are free.

**Wiring:**

- `CLK_PIN` (e.g. `"dio0"`) → SPI clock (SCLK).
- `MOSI_PIN` (e.g. `"dio1"`) → MOSI.
- `MISO_PIN` (e.g. `"dio2"`) → MISO.
- `CS_PIN` (e.g. `"dio3"`) → chip select (active low for most SPI devices).
- `GND` → bus GND.

Loopback test: tie `MISO_PIN` to `MOSI_PIN` — `spi.transfer` echoes the sent bytes back on MISO.

**Tools + sequence:**

1. `spi.configure` — `clk_pin`, `frequency_hz`, `mode` (0–3: CPOL/CPHA), optional `mosi_pin`, `miso_pin`, `cs_pin`, `bit_order` (`"msb"` default or `"lsb"`).
2. `spi.transfer` — `data` (byte list), `assert_cs` (true for automatic CS assertion). Returns `{"received": [...], "byte_count": N}` with the bytes captured on MISO.
3. `spi.write` — write-only (MOSI only, MISO not captured). `data`, `assert_cs`.
4. `spi.read` — read-only (MISO only, drives MOSI low). `n_bytes`, `assert_cs`.

For asymmetric write-then-read with CS held low across both:
```
spi.write(data=[REG_ADDR], assert_cs=false)   # CS asserts, stays low
spi.read(n_bytes=4, assert_cs=true)            # CS deasserts at end
```

**Formulae:** None.

**Interpretation:** `spi.transfer` returns the MISO bytes captured during the MOSI clock-out. The length of `received` equals the length of `data`. For a loopback, `received == data`.

**Gotchas:**

- **SPI protocol engine uses DigitalIn internally** — `spi.configure` (active master) cannot coexist with `sniff.spi_start` (observe-mode DigitalIn) at the same time. Use one or the other per session.
- **Mode selection:** Mode 0 (CPOL=0, CPHA=0) is the most common. Verify your device's datasheet — an incorrect mode causes all bytes to shift incorrectly.
- CS is optional — if `cs_pin` is not specified, you must drive CS manually with `dio.set`.

---
id: protocols:spi-sniff
tools: [waveforms.open, waveforms.list_pins, sniff.spi_start, sniff.spi_status, sniff.spi_stop, decoder.spi]
---

## SPI Passive Sniff

**Goal / when to use:** Observe SPI traffic on a live bus without interfering. The SPI sniffer is **async-only** (`sniff.spi_start/status/stop`) — there is no blocking one-shot `sniff.spi` because the SPI protocol engine uses DigitalIn internally and cannot run concurrently with the active master.

**Preflight:** `waveforms.open`; SPI pins (CLK, MOSI, MISO, CS) connected to the bus. Confirm the SPI active master is NOT configured on the same session — see the SPI/DigitalIn coexistence gotcha in the bench reference.

**Wiring:** Connect CLK, MOSI, MISO, CS DIO pins to the external SPI bus (observe-only, no driving).

**Tools + sequence:**

1. `sniff.spi_start` — `clk_pin`, `mosi_pin`, `miso_pin`, `cs_pin`, `mode` (0–3), `bit_order` (`"msb"` default), `max_duration_s`, optional `stream_decode`. Returns `{"sniff_id": "..."}`.
2. `sniff.spi_status` — poll with `sniff_id`; returns `{"done": bool, "samples_received": int, "lost_samples": int}`. Note: decoded frames are not returned by `sniff.spi_status` — they come from `sniff.spi_stop`.
3. `sniff.spi_stop` — stop and return decoded frames (byte list per transaction) + Parquet artifact path.

**Post-process decode (from a logic record):**

4. `decoder.spi` — `capture_path` (logic NPZ), `clk_pin`, `mosi_pin`, `miso_pin`, `cs_pin`, `mode`, `bit_order`. Decodes SPI transactions from a raw logic NPZ.

**Formulae:** None.

**Interpretation:** The Parquet output has columns for CS-assertion start/end times, MOSI bytes, MISO bytes, and transaction length.

**Gotchas:**

- `sniff.spi_status` reports frame progress (samples, lost) but decoded frames are held until `sniff.spi_stop`.
- CS polarity: the sniffer expects active-low CS (standard). If your device uses active-high CS, frames may not be parsed correctly — check the Parquet output for unexpected splits.

---
id: protocols:uart-master
tools: [waveforms.open, waveforms.list_pins, uart.configure, uart.write, uart.read]
---

## UART Active Master — Send and Receive

**Goal / when to use:** Send and receive bytes over UART from the AD3. Use to talk to a microcontroller debug port, GPS module, BLE module, or any UART-speaking device.

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm TX and RX pins are free.

**Wiring:**

- `TX_PIN` (e.g. `"dio0"`) → RX of the target device (AD3 transmits, target receives).
- `RX_PIN` (e.g. `"dio1"`) → TX of the target device (target transmits, AD3 receives).
- `GND` → shared GND.
- Logic-level voltage: ensure both sides use the same voltage (3.3 V default on AD3 DIO). Use a level shifter if the target is 5 V.

**Jumperless V5 stimulus (RP2350B):** use bare `machine.UART(0, baud)` with no Pin override.

**Tools + sequence:**

1. `uart.configure` — `baud_rate`, optional `tx_pin`, `rx_pin`, `data_bits` (default 8), `parity` (`"none"`, `"even"`, `"odd"`), `stop_bits` (1 or 2).
2. `uart.write` — `data` (byte list). Sends bytes on TX.
3. `uart.read` — `n_bytes`, optional `timeout_s`. Returns received bytes as a list (may be shorter than `n_bytes` if timeout fires).

**Formulae:** Byte transmission time = `(1 + data_bits + stop_bits) / baud_rate` seconds per byte (e.g. 8N1 at 9600 baud ≈ 1.04 ms/byte).

**Interpretation:** `uart.read` returns bytes received within `timeout_s`. An empty list means no data arrived — check baud rate mismatch (the most common cause) and check that TX/RX are not swapped.

**Gotchas:**

- TX and RX are often swapped: AD3 TX → target RX, AD3 RX ← target TX. Verify with a known-working echo before debugging payload content.
- Baud rate mismatch produces garbled bytes (framing errors) — the `uart.read` result will contain 0xFF or misaligned byte values.

---
id: protocols:uart-sniff
tools: [waveforms.open, waveforms.list_pins, sniff.uart, sniff.uart_start, sniff.uart_status, sniff.uart_stop, decoder.uart]
---

## UART Passive Sniff

**Goal / when to use:** Observe UART traffic without interrupting the bus. Two modes: blocking one-shot (`sniff.uart`) and async observe-mode (`sniff.uart_start/status/stop`). The async mode can run concurrently with an active `uart.configure` master.

**Preflight:** `waveforms.open`; RX pin connected to the UART TX line being monitored.

**Wiring:** Connect the RX DIO pin to the TX line of the device being monitored. You are observing only — do not drive TX.

**Tools + sequence — blocking one-shot:**

1. `sniff.uart` — `rx_pin`, `baud_rate`, `duration_s`, optional `data_bits`, `parity`, `stop_bits`. Blocks for `duration_s`, returns decoded frames + Parquet artifact.

**Tools + sequence — async observe-mode:**

1. `sniff.uart_start` — `rx_pin`, `baud_rate`, `max_duration_s`, optional framing params, `stream_decode`. Returns `{"sniff_id": "..."}`.
2. `sniff.uart_status` — poll; returns `{"done": bool, "samples_received": int, "lost_samples": int}`.
3. `sniff.uart_stop` — stop and return decoded frames + Parquet artifact.

**Post-process decode (from a logic record):**

4. `decoder.uart` — `capture_path`, `rx_pin`, `baud_rate`, framing params. Decodes UART bytes from a raw logic NPZ.

**Formulae:** None.

**Interpretation:** The Parquet output has columns for byte value, timestamp, and any framing errors (parity error, stop-bit error). A run of framing errors at a consistent pattern usually means a baud rate mismatch.

**Gotchas:** The blocking `sniff.uart` and the async `sniff.uart_start` both observe on the RX pin; they can coexist with a concurrent UART active master on the TX pin (the engines use separate hardware blocks).

---
id: protocols:can-master
tools: [waveforms.open, waveforms.list_pins, can.configure, can.send, can.receive]
---

## CAN Active Master — Send and Receive Frames

**Goal / when to use:** Send and receive CAN frames from the AD3. Use to exercise a CAN node, inject test frames, or read responses on a CAN bus. The AD3 drives the bus directly (single-ended CANH/CANL differential output via the DIO pins — a CAN transceiver may be needed for longer buses).

**Preflight:** `waveforms.open`; `waveforms.list_pins` to confirm the two CAN DIO pins are free.

**Wiring:**

- `TX_PIN` (e.g. `"dio0"`) → CAN transceiver TXD input (or direct CANH for short benchtop traces).
- `RX_PIN` (e.g. `"dio1"`) → CAN transceiver RXD output.
- Match termination: a 120 Ω resistor at each end of the bus.
- For a self-test loopback (no transceiver): tie TX_PIN to RX_PIN directly on the breadboard.

**Tools + sequence:**

1. `can.configure` — `tx_pin`, `rx_pin`, `bit_rate_hz` (e.g. 500000 for 500 kbit/s standard, 1000000 for 1 Mbit/s).
2. `can.send` — `frame_id` (11-bit standard or 29-bit extended), `data` (byte list, up to 8 bytes), `extended` (true for 29-bit IDs). Transmits one CAN frame.
3. `can.receive` — `timeout_s`. Blocks until one CAN frame arrives (or timeout); returns `{"frame_id": int, "data": [...], "extended": bool, "error_count": int}`.

**Formulae:** CAN bit time = `1 / bit_rate_hz`; a standard 8-byte frame takes roughly 130 bit times (start, 11-bit ID, control, 8×8 data bits, CRC, ACK, end-of-frame).

**Interpretation:** `can.receive` returns one frame per call. For multi-frame traffic, call `can.receive` in a loop. `error_count > 0` indicates bus errors (stuffing error, CRC error) — typically a wiring fault, missing termination, or bit-rate mismatch.

**Gotchas:**

- CAN requires a transceiver for electrically compliant CANH/CANL differential signaling on real bus lengths. For short breadboard loopback tests the single-ended DIO-to-DIO connection works without a transceiver.
- Bit rate must match across all nodes on the bus exactly — even a 1% mismatch will cause persistent errors at long frame lengths.

---
id: protocols:can-sniff
tools: [waveforms.open, waveforms.list_pins, sniff.can, sniff.can_start, sniff.can_status, sniff.can_stop, decoder.can]
---

## CAN Passive Sniff

**Goal / when to use:** Observe CAN traffic without transmitting onto the bus. Two modes: blocking one-shot (`sniff.can`) and async observe-mode (`sniff.can_start/status/stop`). The async mode can coexist with a concurrent `can.configure` active master.

**Preflight:** `waveforms.open`; RX pin connected to the CAN bus (via transceiver RXD, or directly for benchtop).

**Wiring:** Connect the RX DIO pin to the CAN bus RXD signal. Do not connect the TX pin (observe only).

**Tools + sequence — blocking one-shot:**

1. `sniff.can` — `rx_pin`, `bit_rate_hz`, `duration_s`. Blocks and returns decoded frames + Parquet artifact.

**Tools + sequence — async observe-mode:**

1. `sniff.can_start` — `rx_pin`, `bit_rate_hz`, `max_duration_s`, optional `stream_decode`. Returns `{"sniff_id": "..."}`.
2. `sniff.can_status` — poll; returns `{"done": bool, "samples_received": int, "lost_samples": int}`.
3. `sniff.can_stop` — stop and return decoded frames + Parquet artifact.

**Post-process decode (from a logic record):**

4. `decoder.can` — `capture_path`, `rx_pin`, `bit_rate_hz`. Decodes CAN frames from a raw logic NPZ.

**Formulae:** None.

**Interpretation:** The Parquet output has columns for frame ID (standard or extended), data bytes, timestamp, and error flags. Persistent error flags on most frames indicate a bit-rate mismatch or bus wiring issue.

**Gotchas:**

- The blocking `sniff.can` uses the CAN protocol engine (claims it exclusively). The async `sniff.can_start` uses observe-mode DigitalIn and can coexist with a concurrent `can.configure` active master on the same wires — the two engines are independent.
- For a live bus with a real transceiver, ensure the AD3 RX pin is connected to the transceiver's RXD output, not directly to CANH or CANL.
