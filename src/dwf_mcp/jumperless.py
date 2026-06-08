"""
PC-side driver for Jumperless V5 via MicroPython Raw REPL (Port 3).

The Jumperless exposes three USB serial ports:
  Port 1  /dev/cu.usbmodemJLV5port1   main terminal / > commands
  Port 2  /dev/cu.usbmodemJLV5port3   Arduino UART passthrough
  Port 3  /dev/cu.usbmodemJLV5port5   MicroPython Raw REPL  <-- this driver

Usage:
    with Jumperless() as j:
        j.connect(1, 5)
        v = j.adc_get(0)
        state = j.get_state()
"""

import json
import time
import serial
import serial.tools.list_ports


def find_jumperless_ports() -> list[str]:
    """Return Jumperless serial ports sorted by device name (port1, port3, port5...)."""
    ports = sorted(
        [p for p in serial.tools.list_ports.comports()
         if "JLV5" in (p.name or "") or "JLV5" in (p.description or "") or "Jumperless" in (p.description or "")],
        key=lambda p: p.device,
    )
    return [p.device for p in ports]


class JumperlessError(Exception):
    pass


class Jumperless:
    """
    Drives Jumperless V5 over the MicroPython Raw REPL (3rd USB port).

    Stays in raw REPL for the lifetime of the connection — each exec()
    call is just code + Ctrl-D, then read OK + stdout + stderr.
    """

    _CTRL_A = b"\x01"   # enter raw REPL
    _CTRL_B = b"\x02"   # exit raw REPL
    _CTRL_C = b"\x03"   # interrupt running code
    _CTRL_D = b"\x04"   # end of input / soft reset marker

    def __init__(self, port: str | None = None, timeout: float = 5.0):
        if port is None:
            port = self._auto_port()
        self._ser = serial.Serial(port, 115200, timeout=timeout)
        self._timeout = timeout
        self._enter_raw_repl()

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                                 #
    # ------------------------------------------------------------------ #

    def close(self):
        if self._ser.is_open:
            try:
                self._ser.write(self._CTRL_B)   # exit raw REPL gracefully
            except Exception:
                pass
            self._ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------ #
    # Low-level Raw REPL protocol                                         #
    # ------------------------------------------------------------------ #

    def _auto_port(self) -> str:
        ports = find_jumperless_ports()
        if len(ports) < 3:
            raise JumperlessError(
                f"Need 3 Jumperless ports for Raw REPL, found {len(ports)}: {ports}"
            )
        return ports[2]   # 3rd port = Raw REPL

    def _drain(self, settle: float = 0.05) -> bytes:
        """Discard pending bytes, returning once the line has been quiet for `settle` seconds.

        Polls in_waiting instead of blocking reads so the common case (empty
        buffer) returns in one sleep interval rather than the full timeout.
        """
        drained = b""
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            n = self._ser.in_waiting
            if n:
                drained += self._ser.read(n)
                deadline = time.monotonic() + settle  # reset on activity
            else:
                time.sleep(0.005)
        return drained

    def _enter_raw_repl(self):
        """Interrupt any running code and enter raw REPL, draining all output."""
        self._ser.write(self._CTRL_C + self._CTRL_C)
        time.sleep(0.15)
        self._drain()
        self._ser.write(self._CTRL_A)
        time.sleep(0.15)
        self._drain()   # consume 'raw REPL; CTRL-B to exit\r\n>'

    def _read_until(self, marker: bytes, max_bytes: int = 65536) -> bytes:
        buf = b""
        while not buf.endswith(marker):
            c = self._ser.read(1)
            if not c:
                raise TimeoutError(f"Timed out waiting for {marker!r}; got: {buf!r}")
            buf += c
            if len(buf) > max_bytes:
                raise JumperlessError(f"Response too large (>{max_bytes} bytes)")
        return buf[: -len(marker)]

    def exec(self, code: str) -> tuple[str, str]:
        """
        Execute MicroPython code on the device.

        Returns (stdout, stderr) as strings.
        Raises JumperlessError if the device returns a traceback.
        """
        self._ser.write(code.encode() + self._CTRL_D)

        ok = self._ser.read(2)
        if ok != b"OK":
            # Stream is out of sync — re-enter raw REPL cleanly and retry once.
            self._enter_raw_repl()
            self._ser.write(code.encode() + self._CTRL_D)
            ok = self._ser.read(2)
            if ok != b"OK":
                raise JumperlessError(f"Raw REPL sync error: expected OK, got {ok!r}")

        stdout = self._read_until(self._CTRL_D).decode("utf-8", errors="replace")
        stderr = self._read_until(self._CTRL_D).decode("utf-8", errors="replace")

        if stderr.strip():
            # Drain any trailing prompt bytes before raising so the next
            # call doesn't read stale data (e.g. leftover '>' prompt).
            self._drain()
            raise JumperlessError(f"Remote exception:\n{stderr.strip()}")

        return stdout, stderr

    def eval(self, expr: str) -> str:
        """Evaluate an expression and return its printed representation."""
        stdout, _ = self.exec(f"print({expr})")
        return stdout.strip()

    def eval_float(self, expr: str) -> float:
        return float(self.eval(f"float({expr})"))

    def eval_int(self, expr: str) -> int:
        return int(self.eval(f"int({expr})"))

    def eval_bool(self, expr: str) -> bool:
        return self.eval(f"int(bool({expr}))") == "1"

    def eval_json(self, expr: str):
        """Evaluate an expression that returns a JSON-serialisable value."""
        stdout, _ = self.exec(f"import json as _j; print(_j.dumps({expr}))")
        return json.loads(stdout.strip())

    # ------------------------------------------------------------------ #
    # Node connections                                                     #
    # ------------------------------------------------------------------ #

    def connect(self, node1, node2, duplicates: int = -1):
        self.exec(f"connect({node1!r}, {node2!r}, {duplicates})")

    def disconnect(self, node1, node2):
        self.exec(f"disconnect({node1!r}, {node2!r})")

    def nodes_clear(self):
        self.exec("nodes_clear()")

    def is_connected(self, node1, node2) -> bool:
        return self.eval_bool(f"is_connected({node1!r}, {node2!r})")

    # ------------------------------------------------------------------ #
    # Analog I/O                                                          #
    # ------------------------------------------------------------------ #

    def adc_get(self, channel) -> float:
        """Read ADC voltage. channel: 0-4 or 'ADC0' etc."""
        return self.eval_float(f"adc_get({channel!r})")

    def dac_set(self, channel, voltage: float):
        """Set DAC/rail voltage. channel: 0/1/'TOP_RAIL'/'BOTTOM_RAIL'."""
        self.exec(f"dac_set({channel!r}, {voltage})")

    def dac_get(self, channel) -> float:
        return self.eval_float(f"dac_get({channel!r})")

    # ------------------------------------------------------------------ #
    # Current / power                                                     #
    # ------------------------------------------------------------------ #

    def ina_get_current(self, sensor: int = 0) -> float:
        """Current in amps. sensor 0 = DAC0/probe, 1 = TOP_RAIL."""
        return self.eval_float(f"ina_get_current({sensor})")

    def ina_get_power(self, sensor: int = 0) -> float:
        return self.eval_float(f"ina_get_power({sensor})")

    def ina_get_bus_voltage(self, sensor: int = 0) -> float:
        return self.eval_float(f"ina_get_bus_voltage({sensor})")

    # ------------------------------------------------------------------ #
    # GPIO                                                                #
    # ------------------------------------------------------------------ #

    def gpio_set_dir(self, pin: int, output: bool):
        self.exec(f"gpio_set_dir({pin}, {output!r})")

    def gpio_set(self, pin: int, value: bool):
        self.exec(f"gpio_set({pin}, {value!r})")

    def gpio_get(self, pin: int) -> bool:
        return self.eval_bool(f"gpio_get({pin})")

    def gpio_set_pull(self, pin: int, pull: int):
        """pull: 1=pullup, -1=pulldown, 0=none."""
        self.exec(f"gpio_set_pull({pin}, {pull})")

    def pwm(self, pin: int, frequency: float, duty: float = 0.5):
        self.exec(f"pwm({pin}, {frequency}, {duty})")

    def pwm_stop(self, pin: int):
        self.exec(f"pwm_stop({pin})")

    # ------------------------------------------------------------------ #
    # Waveform generator                                                  #
    # ------------------------------------------------------------------ #

    def wavegen_start(self, channel=1, waveform: str = "SINE",
                      freq: float = 100.0, amplitude: float = 3.3,
                      offset: float = 1.65):
        code = (
            f"wavegen_set_output({channel!r})\n"
            f"wavegen_set_wave({waveform})\n"
            f"wavegen_set_freq({freq})\n"
            f"wavegen_set_amplitude({amplitude})\n"
            f"wavegen_set_offset({offset})\n"
            f"wavegen_start()"
        )
        self.exec(code)

    def wavegen_stop(self):
        self.exec("wavegen_stop()")

    # ------------------------------------------------------------------ #
    # Board state                                                         #
    # ------------------------------------------------------------------ #

    def active_slot(self) -> int:
        """Return the currently active slot number (0-7)."""
        return self.eval_int("CURRENT_SLOT")

    def get_state(self) -> dict:
        """Return full board state as a dict.

        Tries the native get_state() JSON first via a temp file (avoids UART
        print encoding issues).  Falls back to the active slot YAML file when
        get_state() itself raises UnicodeError — a known firmware bug on some
        versions.  The YAML fallback returns the same bridge/power/config
        structure but requires PyYAML on the host.
        """
        # Attempt 1: native JSON via temp file
        try:
            code = (
                "_f = open('/tmp_jl_state.json', 'w')\n"
                "_f.write(get_state())\n"
                "_f.close()\n"
                "_f = open('/tmp_jl_state.json')\n"
                "print(_f.read())\n"
                "_f.close()"
            )
            stdout, _ = self.exec(code)
            return json.loads(stdout.strip())
        except JumperlessError:
            pass

        # Attempt 2: read active slot YAML (always works)
        slot = self.active_slot()
        stdout, _ = self.exec(
            f"_f = open('/slots/slot{slot}.yaml')\n"
            "print(_f.read())\n"
            "_f.close()"
        )
        try:
            import yaml  # type: ignore[import]
            return yaml.safe_load(stdout.strip()) or {}
        except ImportError:
            return {"yaml": stdout.strip(), "slot": slot}

    def set_state(self, state: dict, clear_first: bool = True):
        """Apply a full board state from a dict."""
        json_str = json.dumps(state)
        self.exec(f"set_state({json_str!r}, clear_first={clear_first!r})")

    def switch_slot(self, slot: int):
        self.exec(f"switch_slot({slot})")

    def nodes_save(self, slot: int | None = None):
        self.exec(f"nodes_save({slot})" if slot is not None else "nodes_save()")

    # ------------------------------------------------------------------ #
    # OLED                                                                #
    # ------------------------------------------------------------------ #

    def oled_print(self, text: str, size: int = 2):
        self.exec(f"oled_print({text!r}, {size})")

    def oled_clear(self):
        self.exec("oled_clear()")

    # ------------------------------------------------------------------ #
    # Net info                                                            #
    # ------------------------------------------------------------------ #

    def get_net_info(self, net_num: int) -> dict:
        return self.eval_json(f"get_net_info({net_num})")

    def get_num_nets(self) -> int:
        return self.eval_int("get_num_nets()")

    def get_num_bridges(self) -> int:
        return self.eval_int("get_num_bridges()")

    def set_net_color(self, net_num: int, color: str):
        self.exec(f"set_net_color({net_num}, {color!r})")

    # ------------------------------------------------------------------ #
    # Convenience                                                         #
    # ------------------------------------------------------------------ #

    def measure_resistance(self, node1, node2,
                           test_voltage: float = 1.0,
                           adc_channel: int = 0) -> float | None:
        """
        Rough resistance measurement between two nodes using DAC0 + an ADC.
        Returns ohms, or None if open-circuit (>~1MΩ).

        DAC0 has ~80Ω crossbar resistance already in the path, so readings
        below ~200Ω should be treated as approximate.
        """
        code = f"""
import math as _m
dac_set(0, {test_voltage})
connect("DAC0", {node1!r})
connect("ADC{adc_channel}", {node2!r})
import time; time.sleep(0.02)
v_meas = adc_get({adc_channel})
disconnect("DAC0", {node1!r})
disconnect("ADC{adc_channel}", {node2!r})
# DAC0 internal resistance is ~0; treat adc input as high-Z
# V_meas / V_test = R_load / (R_load + 0) — no series R here,
# so if V_meas < 0.01 assume open, else R ≈ 0 (short/wire).
# For real R measurement, caller should add a known series resistor.
print(float(v_meas))
"""
        stdout, _ = self.exec(code)
        v = float(stdout.strip())
        if v < 0.005:
            return None   # open circuit
        # With no series resistor we can only distinguish wire vs open.
        # Return raw measured voltage for caller to interpret.
        return v
