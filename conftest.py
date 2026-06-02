from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m") and "hardware" in config.getoption("-m"):
        return
    skip_hw = pytest.mark.skip(reason="hardware tests require -m hardware")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip_hw)
