from __future__ import annotations

import re

_DIO_PATTERN = re.compile(r"^dio(\d+)$")


def dio_index(pin: str, max_pin: int = 15) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected pin like 'dio0'..'dio{max_pin}', got {pin!r}")
    idx = int(m.group(1))
    if idx > max_pin:
        raise ValueError(f"pin index {idx} out of range (max {max_pin}), got {pin!r}")
    return idx
