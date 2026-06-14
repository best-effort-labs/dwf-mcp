from __future__ import annotations

import re

_DIO_PATTERN = re.compile(r"^dio(\d+)$")


def dio_index(pin: str) -> int:
    m = _DIO_PATTERN.match(pin)
    if not m:
        raise ValueError(f"expected a DIO pin like 'dio0', got {pin!r}")
    return int(m.group(1))
