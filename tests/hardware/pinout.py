from __future__ import annotations

import os

N_PER_SIDE = 15

AD3_TOP_ROW = int(os.environ.get("AD3_TOP_ROW", "1"))
AD3_BOT_ROW = int(os.environ.get("AD3_BOT_ROW", "31"))
# AD3_REVERSED=0 (default): AD3 in natural orientation — connector faces outward, label
#   readable, pins appear in presented order (1+, 2+, GND, V+, W1 … DIO7).
# AD3_REVERSED=1: AD3 faces INTO the breadboard (component side down) — pin order reversed.
AD3_REVERSED = os.environ.get("AD3_REVERSED", "0") == "1"

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
    "UART_TX":      "UART_TX",   # RP2350B GP0, machine.UART(0, tx=Pin(0))
    "UART_RX":      "UART_RX",   # RP2350B GP1, machine.UART(0, rx=Pin(1))
    "GPIO_1":       "GPIO_1",    # RP2350B GP20, machine.I2C(0, sda=Pin(20), ...)
    "GPIO_2":       "GPIO_2",    # RP2350B GP21, machine.I2C(0, scl=Pin(21), ...)
    "GPIO_3":       "GPIO_3",    # RP2350B GP22
    "GPIO_4":       "GPIO_4",    # RP2350B GP23
    # Pre-placed I2C pull-up resistors — direct row numbers (vertical, bridging gap)
    "I2C_SDA_R_A":  28,
    "I2C_SDA_R_B":  58,
    "I2C_SCL_R_A":  29,
    "I2C_SCL_R_B":  59,
}


def row(signal: str) -> int | str:
    entry = _SIGNAL_MAP[signal]
    if isinstance(entry, str):
        return entry
    if isinstance(entry, int):
        return entry
    side, offset = entry
    base = AD3_TOP_ROW if side == "top" else AD3_BOT_ROW
    return base + (N_PER_SIDE - 1 - offset) if AD3_REVERSED else base + offset
