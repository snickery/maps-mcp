"""Shared fixtures for maps-mcp tests."""

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def maps_client(monkeypatch):
    """A MapsClient with a fake key and the three HTTP entrypoints mocked.

    Tests assert on the mock calls (URL / params / body / field mask) and
    inject canned API responses via .return_value.
    """
    from maps_mcp.maps_client import MapsClient

    c = MapsClient(api_key="test-key")
    c._get = MagicMock(name="_get")
    c._post = MagicMock(name="_post")
    c._get_v1 = MagicMock(name="_get_v1")
    return c


@pytest.fixture
def keyless_client():
    """A MapsClient with NO key — for asserting the setup-pointer error."""
    from maps_mcp.maps_client import MapsClient

    return MapsClient(api_key="")


requires_maps = pytest.mark.skipif(
    not os.environ.get("MAPS_API_KEY"),
    reason="Set MAPS_API_KEY to run live Maps Platform integration tests",
)
