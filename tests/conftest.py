"""Shared fixtures. Tests are offline: any socket use outside the opt-in marker fails the test."""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", help="allow public read-only endpoints")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_metered = pytest.mark.skip(reason="metered tests may send orders and are never collected")
    skip_network = pytest.mark.skip(reason="needs --run-network")
    for item in items:
        if "metered" in item.keywords:
            item.add_marker(skip_metered)
        if "network" in item.keywords and not config.getoption("--run-network"):
            item.add_marker(skip_network)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if "network" not in request.keywords:

        def refuse(self: socket.socket, address: object) -> None:
            family = getattr(self, "family", None)
            if family in (socket.AF_INET, socket.AF_INET6):
                host = address[0] if isinstance(address, tuple) else ""
                if host not in ("127.0.0.1", "::1", "localhost"):
                    raise AssertionError(f"offline test attempted network access to {address}")
            return original(self, address)

        original = socket.socket.connect
        monkeypatch.setattr(socket.socket, "connect", refuse)
    yield


@pytest.fixture
def gamma_upcoming() -> dict:
    return json.loads((FIXTURES / "gamma" / "event_upcoming_ctf.json").read_text())[0]


@pytest.fixture
def gamma_closed() -> dict:
    return json.loads((FIXTURES / "gamma" / "event_closed_ctf.json").read_text())[0]


@pytest.fixture
def d() -> type[Decimal]:
    return Decimal
