"""Tier 1 + Tier 2 tests for MapsClient.

Tier 1 covers the pure helpers (waypoint resolution, mode aliases,
duration parsing/formatting, row formatters). Tier 2 exercises the public
methods against mocked HTTP entrypoints — the `maps_client` fixture
replaces _get/_post/_get_v1 so no network calls happen.

Run with:
    uv run --extra test pytest tests/test_maps_client.py -v
"""
from __future__ import annotations

import pytest

from maps_mcp.maps_client import (
    GEOCODE_URL,
    PLACES_SEARCH_URL,
    ROUTES_URL,
    TIMEZONE_URL,
    MapsClient,
    _ROUTES_FIELD_MASK,
    _SEARCH_FIELD_MASK,
)


# ===================================================================
# Tier 1 — pure helpers
# ===================================================================

class TestWaypoint:
    def test_address(self):
        assert MapsClient._waypoint("1 Macquarie St, Sydney") == {
            "address": "1 Macquarie St, Sydney",
        }

    def test_latlng(self):
        assert MapsClient._waypoint("-33.87, 151.21") == {
            "location": {"latLng": {"latitude": -33.87, "longitude": 151.21}},
        }

    def test_place_id(self):
        assert MapsClient._waypoint("place_id:ChIJabc123") == {
            "placeId": "ChIJabc123",
        }

    def test_numeric_street_number_is_address(self):
        # A street address starting with digits must NOT parse as lat,lng.
        assert MapsClient._waypoint("42 Wallaby Way, Sydney") == {
            "address": "42 Wallaby Way, Sydney",
        }

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            MapsClient._waypoint("")


class TestModeResolution:
    @pytest.mark.parametrize("alias,expected", [
        ("drive", "DRIVE"), ("Driving", "DRIVE"), ("car", "DRIVE"),
        ("walk", "WALK"), ("walking", "WALK"),
        ("bike", "BICYCLE"), ("cycling", "BICYCLE"),
        ("transit", "TRANSIT"), ("public_transport", "TRANSIT"),
        ("two_wheeler", "TWO_WHEELER"), ("motorbike", "TWO_WHEELER"),
    ])
    def test_aliases(self, alias, expected):
        assert MapsClient._resolve_mode(alias) == expected

    def test_unknown_rejected(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            MapsClient._resolve_mode("teleport")


class TestDurations:
    def test_parse(self):
        assert MapsClient._parse_duration_s("1234s") == 1234
        assert MapsClient._parse_duration_s("90.5s") == 90
        assert MapsClient._parse_duration_s("") == 0

    def test_format_minutes(self):
        assert MapsClient._format_minutes(240) == "4 min"
        assert MapsClient._format_minutes(3900) == "1 hr 5 min"


class TestFormatters:
    def test_geocode_row(self):
        out = MapsClient._format_geocode_row({
            "formatted_address": "1 Macquarie St, Sydney NSW 2000, Australia",
            "geometry": {"location": {"lat": -33.86, "lng": 151.21}},
            "place_id": "ChIJxyz",
            "types": ["street_address"],
        })
        assert "1 Macquarie St" in out
        assert "-33.86,151.21" in out
        assert "place_id: ChIJxyz" in out

    def test_place_row(self):
        out = MapsClient._format_place_row({
            "id": "ChIJvet",
            "displayName": {"text": "Potts Point Vet"},
            "formattedAddress": "10 Example St, Potts Point NSW",
            "location": {"latitude": -33.868, "longitude": 151.222},
            "rating": 4.8,
            "userRatingCount": 120,
            "currentOpeningHours": {"openNow": True},
            "nationalPhoneNumber": "(02) 9000 0000",
        })
        assert "Potts Point Vet" in out
        assert "rating 4.8 (120)" in out
        assert "OPEN now" in out
        assert "place_id: ChIJvet" in out

    def test_place_row_closed_minimal(self):
        out = MapsClient._format_place_row({
            "id": "x",
            "displayName": {"text": "Shut Shop"},
            "currentOpeningHours": {"openNow": False},
        })
        assert "CLOSED now" in out


class TestKeylessError:
    def test_setup_pointer(self, keyless_client):
        with pytest.raises(ValueError, match="MAPS_API_KEY is not set"):
            keyless_client.geocode("anywhere")


# ===================================================================
# Tier 2 — mocked HTTP
# ===================================================================

class TestGeocode:
    def test_request_and_format(self, maps_client):
        maps_client._get.return_value = {"status": "OK", "results": [{
            "formatted_address": "Sydney NSW, Australia",
            "geometry": {"location": {"lat": -33.87, "lng": 151.21}},
            "place_id": "ChIJsyd",
            "types": ["locality"],
        }]}
        out = maps_client.geocode("Sydney")
        url, params = maps_client._get.call_args.args
        assert url == GEOCODE_URL
        assert params["address"] == "Sydney"
        assert "region" not in params  # no bias unless MAPS_REGION is set
        assert "ChIJsyd" in out

    def test_region_override(self, maps_client):
        maps_client._get.return_value = {"status": "OK", "results": []}
        out = maps_client.geocode("Springfield", region="us")
        assert maps_client._get.call_args.args[1]["region"] == "us"
        assert "No geocoding results" in out

    def test_region_env_bias(self, maps_client, monkeypatch):
        import maps_mcp.maps_client as mc

        monkeypatch.setattr(mc, "MAPS_REGION", "au")
        maps_client._get.return_value = {"status": "OK", "results": []}
        maps_client.geocode("Sydney")
        assert maps_client._get.call_args.args[1]["region"] == "au"

    def test_empty_address_rejected(self, maps_client):
        with pytest.raises(ValueError, match="must not be empty"):
            maps_client.geocode("")


class TestReverseGeocode:
    def test_request(self, maps_client):
        maps_client._get.return_value = {"status": "OK", "results": [{
            "formatted_address": "1 Test St",
            "geometry": {"location": {"lat": -33.9, "lng": 151.2}},
            "place_id": "p1",
        }]}
        out = maps_client.reverse_geocode(-33.9, 151.2)
        assert maps_client._get.call_args.args[1]["latlng"] == "-33.9,151.2"
        assert "1 Test St" in out


class TestPlaceSearch:
    def test_minimal_body(self, maps_client):
        maps_client._post.return_value = {"places": [{
            "id": "p1", "displayName": {"text": "Cafe"},
        }]}
        out = maps_client.place_search("coffee Surry Hills")
        url, body, mask = maps_client._post.call_args.args
        assert url == PLACES_SEARCH_URL
        assert body["textQuery"] == "coffee Surry Hills"
        assert body["pageSize"] == 5
        assert "openNow" not in body
        assert "locationBias" not in body
        assert mask == _SEARCH_FIELD_MASK
        assert "Cafe" in out

    def test_bias_and_filters(self, maps_client):
        maps_client._post.return_value = {"places": []}
        out = maps_client.place_search(
            "vet", latitude=-33.87, longitude=151.22,
            radius_meters=2000, open_now=True, max_results=3,
        )
        body = maps_client._post.call_args.args[1]
        assert body["openNow"] is True
        assert body["pageSize"] == 3
        assert body["locationBias"]["circle"]["radius"] == 2000.0
        assert body["locationBias"]["circle"]["center"]["latitude"] == -33.87
        assert "No places matching" in out

    def test_max_results_clamped(self, maps_client):
        maps_client._post.return_value = {"places": []}
        maps_client.place_search("x", max_results=99)
        assert maps_client._post.call_args.args[1]["pageSize"] == 20


class TestPlaceDetails:
    def test_request_and_format(self, maps_client):
        maps_client._get_v1.return_value = {
            "id": "ChIJvet",
            "displayName": {"text": "Potts Point Vet"},
            "formattedAddress": "10 Example St",
            "location": {"latitude": -33.868, "longitude": 151.222},
            "rating": 4.8,
            "userRatingCount": 120,
            "currentOpeningHours": {"openNow": False},
            "regularOpeningHours": {"weekdayDescriptions": [
                "Monday: 8:00 AM – 6:00 PM",
            ]},
            "internationalPhoneNumber": "+61 2 9000 0000",
            "nationalPhoneNumber": "(02) 9000 0000",
            "websiteUri": "https://vet.example",
            "types": ["veterinary_care"],
        }
        out = maps_client.place_details("ChIJvet")
        args = maps_client._get_v1.call_args
        assert args.args[0].endswith("/places/ChIJvet")
        assert "Monday: 8:00" in out
        assert "open now: no" in out
        # International wins; national must not double-print.
        assert out.count("9000 0000") == 1
        assert "+61 2 9000 0000" in out

    def test_empty_id_rejected(self, maps_client):
        with pytest.raises(ValueError, match="must not be empty"):
            maps_client.place_details("")


class TestTravelTime:
    def _route(self, duration="1500s", static="1200s", meters=12000):
        return {"routes": [{
            "duration": duration,
            "staticDuration": static,
            "distanceMeters": meters,
        }]}

    def test_drive_traffic_aware(self, maps_client):
        maps_client._post.return_value = self._route()
        out = maps_client.travel_time("Potts Point", "Manly")
        url, body, mask = maps_client._post.call_args.args
        assert url == ROUTES_URL
        assert body["travelMode"] == "DRIVE"
        assert body["routingPreference"] == "TRAFFIC_AWARE"
        assert body["origin"] == {"address": "Potts Point"}
        assert mask == _ROUTES_FIELD_MASK
        assert "25 min" in out
        assert "12.0 km" in out
        assert "+5 min" in out  # 1500 vs 1200 baseline

    def test_transit_no_routing_preference(self, maps_client):
        maps_client._post.return_value = self._route(static="")
        maps_client.travel_time("A", "B", mode="transit")
        body = maps_client._post.call_args.args[1]
        assert body["travelMode"] == "TRANSIT"
        assert "routingPreference" not in body
        assert "routeModifiers" not in body

    def test_avoid_tolls_drive_only(self, maps_client):
        maps_client._post.return_value = self._route()
        maps_client.travel_time("A", "B", avoid_tolls=True)
        body = maps_client._post.call_args.args[1]
        assert body["routeModifiers"] == {"avoidTolls": True}

    def test_departure_time_passthrough(self, maps_client):
        maps_client._post.return_value = self._route()
        out = maps_client.travel_time(
            "A", "B", departure_time="2026-07-05T08:30:00+10:00",
        )
        body = maps_client._post.call_args.args[1]
        assert body["departureTime"] == "2026-07-05T08:30:00+10:00"
        assert "departing: 2026-07-05T08:30:00+10:00" in out

    def test_no_route(self, maps_client):
        maps_client._post.return_value = {"routes": []}
        assert "No route found" in maps_client.travel_time("A", "B")

    def test_negligible_traffic(self, maps_client):
        maps_client._post.return_value = self._route(
            duration="1230s", static="1200s",
        )
        out = maps_client.travel_time("A", "B")
        assert "none to speak of" in out


class TestTimeZone:
    def test_request_and_format(self, maps_client):
        maps_client._get.return_value = {
            "status": "OK",
            "timeZoneId": "Australia/Sydney",
            "timeZoneName": "Australian Eastern Standard Time",
            "rawOffset": 36000,
            "dstOffset": 0,
        }
        out = maps_client.time_zone(-33.87, 151.21)
        url, params = maps_client._get.call_args.args
        assert url == TIMEZONE_URL
        assert params["location"] == "-33.87,151.21"
        assert params["timestamp"] > 0
        assert "Australia/Sydney" in out
        assert "UTC+10" in out

    def test_dst_offset_included(self, maps_client):
        maps_client._get.return_value = {
            "status": "OK",
            "timeZoneId": "Australia/Sydney",
            "timeZoneName": "AEDT",
            "rawOffset": 36000,
            "dstOffset": 3600,
        }
        out = maps_client.time_zone(-33.87, 151.21, timestamp=1735689600)
        assert maps_client._get.call_args.args[1]["timestamp"] == 1735689600
        assert "UTC+11" in out
        assert "incl. DST" in out


# ===================================================================
# travel_time transit/toll additions (2026-08 gap closure)
# ===================================================================

class TestTravelTimeTransit:
    def test_arrival_time_transit_only(self, maps_client):
        with pytest.raises(ValueError, match="transit"):
            maps_client.travel_time("A", "B", mode="drive",
                                    arrival_time="2026-08-06T09:00:00+10:00")

    def test_arrival_time_sent_with_transit_mask(self, maps_client):
        maps_client._post.return_value = {"routes": [{
            "duration": "1800s", "distanceMeters": 9000,
            "legs": [{"steps": [{"travelMode": "TRANSIT", "transitDetails": {
                "transitLine": {"nameShort": "333",
                                "vehicle": {"type": "BUS"}},
                "stopDetails": {
                    "departureStop": {"name": "Stop A"},
                    "arrivalStop": {"name": "Stop B"},
                    "departureTime": "2026-08-06T08:31:00Z",
                    "arrivalTime": "2026-08-06T08:52:00Z",
                },
                "stopCount": 12,
            }}]}],
        }]}
        out = maps_client.travel_time(
            "A", "B", mode="transit",
            arrival_time="2026-08-06T09:00:00+10:00",
        )
        body = maps_client._post.call_args.args[1]
        mask = maps_client._post.call_args.args[2]
        assert body["arrivalTime"] == "2026-08-06T09:00:00+10:00"
        assert "routingPreference" not in body
        assert "transitDetails" in mask
        assert "333 (bus) Stop A 08:31 → Stop B 08:52, 12 stops" in out
        assert "arriving by:" in out

    def test_transit_mask_without_arrival(self, maps_client):
        maps_client._post.return_value = {"routes": [{"duration": "600s"}]}
        maps_client.travel_time("A", "B", mode="transit")
        assert "transitDetails" in maps_client._post.call_args.args[2]


class TestTravelTimeTolls:
    def test_tolls_opt_in(self, maps_client):
        maps_client._post.return_value = {"routes": [{
            "duration": "1200s", "staticDuration": "1100s",
            "distanceMeters": 20000,
            "travelAdvisory": {"tollInfo": {"estimatedPrice": [
                {"currencyCode": "AUD", "units": "7", "nanos": 380000000},
            ]}},
        }]}
        out = maps_client.travel_time("A", "B", include_tolls=True)
        body = maps_client._post.call_args.args[1]
        mask = maps_client._post.call_args.args[2]
        assert body["extraComputations"] == ["TOLLS"]
        assert "tollInfo" in mask
        assert "tolls:    ~7.38 AUD" in out

    def test_tolls_off_by_default(self, maps_client):
        maps_client._post.return_value = {"routes": [{"duration": "600s"}]}
        maps_client.travel_time("A", "B")
        assert "extraComputations" not in maps_client._post.call_args.args[1]
        assert "tollInfo" not in maps_client._post.call_args.args[2]


# ===================================================================
# place_search_nearby (2026-08 gap closure)
# ===================================================================

class TestPlaceSearchNearby:
    def test_request_shape(self, maps_client):
        maps_client._post.return_value = {"places": [{
            "id": "p1", "displayName": {"text": "Chemist"},
            "formattedAddress": "1 Example St",
        }]}
        out = maps_client.place_search_nearby(
            -33.87, 151.22, "pharmacy", radius_meters=800,
            max_results=3, rank_by_distance=True,
        )
        url, body, mask = maps_client._post.call_args.args
        assert url.endswith(":searchNearby")
        assert body["maxResultCount"] == 3  # nearby keeps maxResultCount
        assert body["includedTypes"] == ["pharmacy"]
        assert body["rankPreference"] == "DISTANCE"
        circle = body["locationRestriction"]["circle"]
        assert circle["radius"] == 800.0
        assert "Chemist" in out

    def test_no_types_no_rank(self, maps_client):
        maps_client._post.return_value = {"places": []}
        out = maps_client.place_search_nearby(-33.87, 151.22)
        body = maps_client._post.call_args.args[1]
        assert "includedTypes" not in body
        assert "rankPreference" not in body
        assert "No places" in out
