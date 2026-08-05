"""Tier 3 integration tests — hit the real Google Maps Platform APIs.

Skipped unless MAPS_API_KEY is set. Everything here is read-only (Maps
has no user data), so there is nothing to create or clean up; the tests
assert on stable Sydney landmarks without pinning volatile values like
ratings or live traffic durations.

Run with:
    MAPS_API_KEY=... uv run --extra test pytest tests/test_integration.py -v

Requires the Geocoding, Places (New), Routes, and Time Zone APIs to be
enabled on the key's Google Cloud project.
"""
from __future__ import annotations

import pytest

from tests.conftest import requires_maps


@pytest.fixture(scope="module")
def live_client():
    from maps_mcp.maps_client import MapsClient
    return MapsClient()


@requires_maps
class TestLiveMaps:
    def test_geocode_round_trip(self, live_client):
        out = live_client.geocode("Sydney Opera House")
        assert "place_id:" in out
        assert "-33.8" in out  # right hemisphere, right city

    def test_reverse_geocode(self, live_client):
        out = live_client.reverse_geocode(-33.8568, 151.2153)
        assert "NSW" in out

    def test_place_search_and_details(self, live_client):
        out = live_client.place_search("cafe near Circular Quay Sydney", max_results=3)
        assert "place_id:" in out
        place_id = out.split("place_id: ")[1].split()[0]
        detail = live_client.place_details(place_id)
        assert "Place:" in detail
        assert "address:" in detail

    def test_travel_time_drive(self, live_client):
        out = live_client.travel_time(
            "Sydney Opera House", "Bondi Beach", mode="drive",
        )
        assert "min" in out
        assert "km" in out

    def test_travel_time_transit(self, live_client):
        out = live_client.travel_time(
            "Central Station Sydney", "Circular Quay Sydney", mode="transit",
        )
        assert "min" in out

    def test_time_zone(self, live_client):
        out = live_client.time_zone(-33.8568, 151.2153)
        assert "Australia/Sydney" in out
