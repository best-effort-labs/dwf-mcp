from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--jumperless-manual",
        action="store_true",
        help="Force manual wiring prompts even if Jumperless device is found",
    )
    parser.addoption(
        "--skip-wiring-prompts",
        action="store_true",
        help="Skip all wiring prompts — for CI or pre-wired bench",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "jumperless(connections): dict of label -> (signal1, signal2) connections required",
    )
