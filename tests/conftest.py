"""Shared test setup: make the project importable and block real DNS lookups."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402


@pytest.fixture(autouse=True)
def fake_public_dns(monkeypatch):
    """Every hostname 'resolves' to a public IP, so tests never touch the network."""
    monkeypatch.setattr(monitor, "_resolve_ips", lambda hostname: ["93.184.216.34"])
