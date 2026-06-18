from __future__ import annotations

import os

N_PER_SIDE = 15

AD3_TOP_ROW = int(os.environ.get("AD3_TOP_ROW", "1"))
AD3_BOT_ROW = int(os.environ.get("AD3_BOT_ROW", "31"))
# AD3_REVERSED=0 (default): AD3 in natural orientation — connector faces outward, label
#   readable, pins appear in presented order (1+, 2+, GND, V+, W1 … DIO7).
# AD3_REVERSED=1: AD3 faces INTO the breadboard (component side down) — pin order reversed.
AD3_REVERSED = os.environ.get("AD3_REVERSED", "0") == "1"

# --- Analog Discovery Pro 2230 (2x16 MTE digital header) ---
# Plugged face-in on the right of the board (reversed pin order), so the rows don't
# follow the simple base+offset model — these are the measured Jumperless rows for
# the bench wiring (DIO0/DIO1 on the top row, GND the ↓ pin next to DIO0).
# Override via env to relocate.
ADP_DIO0_ROW = int(os.environ.get("ADP_DIO0_ROW", "29"))
ADP_DIO1_ROW = int(os.environ.get("ADP_DIO1_ROW", "28"))
ADP_GND_ROW = int(os.environ.get("ADP_GND_ROW", "30"))

# --- Digital Discovery (left side connector, DIO24-31) ---
N_DD_PER_SIDE = 6
DD_LEFT_TOP_ROW = int(os.environ.get("DD_LEFT_TOP_ROW", "1"))  # row for datasheet pin 1 (top VIO)
DD_LEFT_BOT_ROW = int(os.environ.get("DD_LEFT_BOT_ROW", "60"))  # row of bottom-row VIO
# DD_REVERSED=1: connector plugged the other way — each row's count direction flips.
DD_REVERSED = os.environ.get("DD_REVERSED", "0") == "1"

# Offsets are DATASHEET positions (0 = pin 1 per datasheet).
_SIGNAL_MAP: dict[str, tuple[str, int] | str | int] = {
    # Scope inputs — top row physical positions 0,1 → datasheet offsets 14,13
    "CH1_POS":  ("top", 14),
    "CH2_POS":  ("top", 13),  # row 14
    # Scope inputs — bottom row
    "CH1_NEG":  ("bot", 14),
    "CH2_NEG":  ("bot", 13),
    # AWG outputs
    "W1":       ("top", 10),
    "W2":       ("bot", 10),
    # Triggers
    "TRIG_IN":  ("top", 8),
    "TRIG_OUT": ("bot", 8),
    # Power / reference pins on the AD3 header
    "AD3_GND":  ("top", 12),   # AD3 GND pin (top row, between 2+ and V+) → row 13
    "AD3_VPLUS": ("top", 11),  # AD3 V+ pin → row 12
    # Power — map to Jumperless rail aliases instead of breadboard rows
    "VCC":      "TOP_RAIL",
    # Digital I/O, top row (physical positions 7-14 → offsets 7-0)
    "DIO0":     ("top", 7),
    "DIO1":     ("top", 6),
    "DIO2":     ("top", 5),
    "DIO3":     ("top", 4),
    "DIO4":     ("top", 3),
    "DIO5":     ("top", 2),
    "DIO6":     ("top", 1),
    "DIO7":     ("top", 0),
    # Digital I/O, bottom row (physical positions 7-14 → offsets 7-0)
    "DIO8":     ("bot", 7),
    "DIO9":     ("bot", 6),
    "DIO10":    ("bot", 5),
    "DIO11":    ("bot", 4),
    "DIO12":    ("bot", 3),
    "DIO13":    ("bot", 2),
    "DIO14":    ("bot", 1),
    "DIO15":    ("bot", 0),
    # Digital Discovery left side connector. Offsets = datasheet position from pin 1.
    # dd_left_top counts UP from DD_LEFT_TOP_ROW; dd_left_bot counts DOWN from DD_LEFT_BOT_ROW.
    "DD_VIO":   ("dd_left_top", 0),
    "DD_GND":   ("dd_left_top", 1),
    "DIO27":    ("dd_left_top", 2),
    "DIO26":    ("dd_left_top", 3),
    "DIO25":    ("dd_left_top", 4),
    "DIO24":    ("dd_left_top", 5),
    "DD_VIO_B": ("dd_left_bot", 0),
    "DD_GND_B": ("dd_left_bot", 1),
    "DIO31":    ("dd_left_bot", 2),
    "DIO30":    ("dd_left_bot", 3),
    "DIO29":    ("dd_left_bot", 4),
    "DIO28":    ("dd_left_bot", 5),
    # ADP2230 digital header (2x16 MTE) — direct measured rows for the bench wiring.
    "ADP_DIO0":  ADP_DIO0_ROW,
    "ADP_DIO1":  ADP_DIO1_ROW,
    "ADP_GND":   ADP_GND_ROW,
    # Jumperless built-in node aliases — pass through as strings
    "GND":          "GND",
    "TOP_RAIL":     "TOP_RAIL",
    "BOTTOM_RAIL":  "BOTTOM_RAIL",
    "DAC0":         "DAC0",
    "DAC1":         "DAC1",
    "ADC0":         "ADC0",
    "ADC1":         "ADC1",
    "ADC2":         "ADC2",
    "ADC3":         "ADC3",
    "ADC4":         "ADC4",
    # RP2350B routable GPIO / UART — pass through as string node aliases
    # UART_TX/UART_RX are wired by the Jumperless firmware to UART(0)'s default
    # MicroPython pins. Use `machine.UART(0, baudrate)` with NO Pin() override
    # (see scripts/ex/uart_loopback.py in JumperlOS).
    "UART_TX":      "UART_TX",
    "UART_RX":      "UART_RX",
    # GPIO_1..GPIO_8 route to physical RP2350B GP20..GP27 (see JumperlOS docs).
    "GPIO_1":       "GPIO_1",    # GP20
    "GPIO_2":       "GPIO_2",    # GP21
    "GPIO_3":       "GPIO_3",    # GP22
    "GPIO_4":       "GPIO_4",    # GP23
    # Pre-placed I2C pull-up resistors — direct row numbers (vertical, bridging gap)
    # 10kΩ resistors. SCL uses rows 30/60; SDA uses rows 28/58.
    # (Row 29/59 has a 100Ω resistor, unused — too low for I2C pull-up.)
    "I2C_SDA_R_A":  28,
    "I2C_SDA_R_B":  58,
    "I2C_SCL_R_A":  30,
    "I2C_SCL_R_B":  60,
}


def row(signal: str) -> int | str:
    entry = _SIGNAL_MAP[signal]
    if isinstance(entry, str):
        return entry
    if isinstance(entry, int):
        return entry
    side, offset = entry
    if side == "dd_left_top":
        off = (N_DD_PER_SIDE - 1 - offset) if DD_REVERSED else offset
        return DD_LEFT_TOP_ROW + off
    if side == "dd_left_bot":
        off = (N_DD_PER_SIDE - 1 - offset) if DD_REVERSED else offset
        return DD_LEFT_BOT_ROW - off
    base = AD3_TOP_ROW if side == "top" else AD3_BOT_ROW
    return base + (N_PER_SIDE - 1 - offset) if AD3_REVERSED else base + offset
