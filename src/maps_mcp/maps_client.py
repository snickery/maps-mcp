"""Google Maps Platform client for maps-mcp.

Talks to four APIs with one httpx client (thread-safe, so no locking is
needed under the server's threadpool):

* **Geocoding API** (legacy-style, key as query param) — geocode /
  reverse_geocode.
* **Time Zone API** (legacy-style) — time_zone.
* **Places API (New)** (key + field mask headers) — place_search /
  place_details. The field mask is mandatory; requests without one 400.
* **Routes API** (key + field mask headers) — travel_time, traffic-aware
  for driving modes.

Design notes
------------
* Auth is an API key, not OAuth — there is no account dimension here.
  A missing key raises a setup-pointer error per call; the server itself
  starts fine without it so the deployment can precede the key.
* Origins/destinations accept three spellings, resolved by shape:
  ``"lat,lng"`` (two floats), ``"place_id:<id>"``, or any other string as
  a free-form address.
* Formatters return human-readable strings — compact, LLM-friendly
  summaries rather than raw JSON.
"""
from __future__ import annotations

import re

import httpx

from maps_mcp.config import MAPS_API_KEY, MAPS_LANGUAGE, MAPS_REGION

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json"
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

_HTTP_TIMEOUT = 30

_LATLNG_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)

_TRAVEL_MODES = {
    "drive": "DRIVE",
    "driving": "DRIVE",
    "car": "DRIVE",
    "walk": "WALK",
    "walking": "WALK",
    "bicycle": "BICYCLE",
    "bike": "BICYCLE",
    "cycling": "BICYCLE",
    "transit": "TRANSIT",
    "public_transport": "TRANSIT",
    "two_wheeler": "TWO_WHEELER",
    "motorbike": "TWO_WHEELER",
}

# Only these modes accept a routingPreference; TRANSIT/WALK/BICYCLE 400 on it.
_TRAFFIC_MODES = {"DRIVE", "TWO_WHEELER"}

_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.currentOpeningHours.openNow,"
    "places.nationalPhoneNumber,places.websiteUri,places.businessStatus"
)
_DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,location,rating,userRatingCount,"
    "currentOpeningHours.openNow,regularOpeningHours.weekdayDescriptions,"
    "internationalPhoneNumber,nationalPhoneNumber,websiteUri,googleMapsUri,"
    "businessStatus,priceLevel,types,editorialSummary"
)
_ROUTES_FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
# Transit answers need the leg steps; tolls need the travel advisory. Both
# are requested only when relevant so the base mask stays cheap.
_TRANSIT_FIELD_MASK = (
    _ROUTES_FIELD_MASK
    + ",routes.legs.steps.transitDetails,routes.legs.steps.travelMode"
)
_TOLLS_FIELD_MASK = _ROUTES_FIELD_MASK + ",routes.travelAdvisory.tollInfo"


class MapsClient:
    def __init__(self, api_key: str | None = None, timeout: int = _HTTP_TIMEOUT):
        # api_key=None means "use the env-configured key"; tests inject one.
        self._env_key = api_key
        self._client = httpx.Client(timeout=timeout)

    @property
    def api_key(self) -> str:
        key = self._env_key if self._env_key is not None else MAPS_API_KEY
        if not key:
            raise ValueError(
                "MAPS_API_KEY is not set. Create an API key in the Google "
                "Cloud console (with the Geocoding, Places, Routes, and Time "
                "Zone APIs enabled) and set it in the server's env file — "
                "see the maps-mcp README § Setup."
            )
        return key

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _redact_key(text: str) -> str:
        """Strip API-key query values from error text before it can propagate."""
        return re.sub(r"(key=)[^&\s'\"]+", r"\1REDACTED", text)

    def _get(self, url: str, params: dict) -> dict:
        """Legacy-style Maps API GET (key as query param, status in body)."""
        params = {**params, "key": self.api_key}
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as e:
            # The legacy APIs carry the key in the query string; an httpx
            # error message can stringify the URL, so redact before raising.
            raise RuntimeError(
                f"Maps API request failed: {self._redact_key(str(e))}"
            ) from None
        if resp.status_code != 200:
            raise RuntimeError(
                f"Maps API HTTP {resp.status_code}: {self._redact_key(resp.text[:500])}"
            )
        data = resp.json()
        status = data.get("status", "")
        if status not in ("OK", "ZERO_RESULTS"):
            msg = data.get("error_message", "")
            raise RuntimeError(f"Maps API status {status}: {msg}".rstrip(": "))
        return data

    def _post(self, url: str, body: dict, field_mask: str) -> dict:
        """New-style Maps API POST (key + field mask headers, errors as JSON)."""
        resp = self._client.post(url, json=body, headers={
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Maps API HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _get_v1(self, url: str, field_mask: str, params: dict | None = None) -> dict:
        """New-style Maps API GET (Places details)."""
        resp = self._client.get(url, params=params or {}, headers={
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Maps API HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    # ------------------------------------------------------------------
    # Pure helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _waypoint(value: str) -> dict:
        """Resolve an origin/destination string into a Routes API waypoint."""
        if not value:
            raise ValueError("origin/destination must not be empty")
        m = _LATLNG_RE.match(value)
        if m:
            return {"location": {"latLng": {
                "latitude": float(m.group(1)),
                "longitude": float(m.group(2)),
            }}}
        if value.startswith("place_id:"):
            return {"placeId": value[len("place_id:"):]}
        return {"address": value}

    @staticmethod
    def _resolve_mode(mode: str) -> str:
        key = mode.strip().lower()
        if key not in _TRAVEL_MODES:
            raise ValueError(
                f"Unknown mode '{mode}'. Use one of: drive, walk, bicycle, "
                f"transit, two_wheeler (or a common alias)."
            )
        return _TRAVEL_MODES[key]

    @staticmethod
    def _parse_duration_s(value: str) -> int:
        """Routes API durations are strings like '1234s'."""
        return int(float(value.rstrip("s"))) if value else 0

    @staticmethod
    def _format_minutes(seconds: int) -> str:
        minutes = round(seconds / 60)
        if minutes < 60:
            return f"{minutes} min"
        return f"{minutes // 60} hr {minutes % 60} min"

    @staticmethod
    def _format_geocode_row(result: dict) -> str:
        loc = result.get("geometry", {}).get("location", {})
        types = ",".join(result.get("types", [])[:3])
        return (
            f"  • {result.get('formatted_address', '?')}\n"
            f"    lat,lng: {loc.get('lat', '?')},{loc.get('lng', '?')}"
            f"  place_id: {result.get('place_id', '')}"
            + (f"  [{types}]" if types else "")
        )

    @staticmethod
    def _format_place_row(place: dict) -> str:
        name = place.get("displayName", {}).get("text", "(unnamed)")
        lines = [f"  • {name}"]
        if place.get("formattedAddress"):
            lines.append(f"    {place['formattedAddress']}")
        bits = []
        if place.get("rating") is not None:
            bits.append(
                f"rating {place['rating']}"
                + (f" ({place.get('userRatingCount')})" if place.get("userRatingCount") else "")
            )
        open_now = place.get("currentOpeningHours", {}).get("openNow")
        if open_now is not None:
            bits.append("OPEN now" if open_now else "CLOSED now")
        if place.get("nationalPhoneNumber"):
            bits.append(place["nationalPhoneNumber"])
        if place.get("businessStatus") and place["businessStatus"] != "OPERATIONAL":
            bits.append(place["businessStatus"])
        if bits:
            lines.append("    " + "  |  ".join(bits))
        loc = place.get("location", {})
        lines.append(
            f"    lat,lng: {loc.get('latitude', '?')},{loc.get('longitude', '?')}"
            f"  place_id: {place.get('id', '')}"
        )
        if place.get("websiteUri"):
            lines.append(f"    {place['websiteUri']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # geocode / reverse_geocode
    # ------------------------------------------------------------------
    def geocode(self, address: str, region: str = "") -> str:
        if not address:
            raise ValueError("address must not be empty")
        params = {"address": address}
        if region or MAPS_REGION:
            params["region"] = region or MAPS_REGION
        data = self._get(GEOCODE_URL, params)
        results = data.get("results", [])
        if not results:
            return f"No geocoding results for '{address}'."
        lines = [f"{len(results)} result(s) for '{address}':"]
        for r in results[:5]:
            lines.append(self._format_geocode_row(r))
        return "\n".join(lines)

    def reverse_geocode(self, latitude: float, longitude: float) -> str:
        data = self._get(GEOCODE_URL, {"latlng": f"{latitude},{longitude}"})
        results = data.get("results", [])
        if not results:
            return f"No address found at {latitude},{longitude}."
        lines = [f"Address(es) at {latitude},{longitude}:"]
        for r in results[:3]:
            lines.append(self._format_geocode_row(r))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # place_search / place_details
    # ------------------------------------------------------------------
    def place_search(
        self,
        query: str,
        latitude: float = 0.0,
        longitude: float = 0.0,
        radius_meters: int = 0,
        open_now: bool = False,
        max_results: int = 5,
    ) -> str:
        if not query:
            raise ValueError("query must not be empty")
        body: dict = {
            "textQuery": query,
            "pageSize": max(1, min(max_results, 20)),
        }
        if MAPS_LANGUAGE:
            body["languageCode"] = MAPS_LANGUAGE
        if open_now:
            body["openNow"] = True
        if latitude or longitude:
            body["locationBias"] = {"circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": float(radius_meters or 5000),
            }}
        data = self._post(PLACES_SEARCH_URL, body, _SEARCH_FIELD_MASK)
        places = data.get("places", [])
        if not places:
            return f"No places matching '{query}'."
        lines = [f"{len(places)} place(s) for '{query}':"]
        for p in places:
            lines.append(self._format_place_row(p))
        return "\n".join(lines)

    def place_search_nearby(
        self,
        latitude: float,
        longitude: float,
        included_types: str = "",
        radius_meters: int = 1500,
        max_results: int = 5,
        rank_by_distance: bool = False,
    ) -> str:
        """Typed nearby search (Places New searchNearby).

        Unlike searchText, this endpoint still uses maxResultCount (only
        Text Search moved to pageSize) and REQUIRES locationRestriction.
        """
        body: dict = {
            "maxResultCount": max(1, min(max_results, 20)),
            "locationRestriction": {"circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": float(max(1, min(radius_meters, 50000))),
            }},
        }
        types = [t.strip() for t in included_types.split(",") if t.strip()]
        if types:
            body["includedTypes"] = types
        if rank_by_distance:
            body["rankPreference"] = "DISTANCE"
        if MAPS_LANGUAGE:
            body["languageCode"] = MAPS_LANGUAGE
        data = self._post(PLACES_NEARBY_URL, body, _SEARCH_FIELD_MASK)
        places = data.get("places", [])
        what = " / ".join(types) if types else "places"
        if not places:
            return (
                f"No {what} within {radius_meters} m of "
                f"{latitude},{longitude}."
            )
        lines = [
            f"{len(places)} {what} near {latitude},{longitude} "
            f"(within {radius_meters} m):"
        ]
        for pl in places:
            lines.append(self._format_place_row(pl))
        return "\n".join(lines)

    def place_details(self, place_id: str) -> str:
        if not place_id:
            raise ValueError("place_id must not be empty")
        place = self._get_v1(
            PLACES_DETAILS_URL + place_id,
            _DETAILS_FIELD_MASK,
            params={"languageCode": MAPS_LANGUAGE} if MAPS_LANGUAGE else None,
        )
        name = place.get("displayName", {}).get("text", "(unnamed)")
        lines = [f"Place: {name}", f"  place_id: {place.get('id', '')}"]
        if place.get("formattedAddress"):
            lines.append(f"  address:  {place['formattedAddress']}")
        loc = place.get("location", {})
        if loc:
            lines.append(
                f"  lat,lng:  {loc.get('latitude', '?')},{loc.get('longitude', '?')}"
            )
        if place.get("rating") is not None:
            lines.append(
                f"  rating:   {place['rating']}"
                + (f" ({place.get('userRatingCount')} reviews)" if place.get("userRatingCount") else "")
            )
        open_now = place.get("currentOpeningHours", {}).get("openNow")
        if open_now is not None:
            lines.append(f"  open now: {'yes' if open_now else 'no'}")
        for d in place.get("regularOpeningHours", {}).get("weekdayDescriptions", []):
            lines.append(f"    {d}")
        phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
        if phone:
            lines.append(f"  phone:    {phone}")
        for k, label in (
            ("websiteUri", "website"),
            ("googleMapsUri", "maps"),
            ("businessStatus", "status"),
            ("priceLevel", "price"),
        ):
            if place.get(k):
                lines.append(f"  {label + ':':9} {place[k]}")
        if place.get("editorialSummary", {}).get("text"):
            lines.append(f"  summary:  {place['editorialSummary']['text']}")
        if place.get("types"):
            lines.append(f"  types:    {', '.join(place['types'][:6])}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # travel_time — Routes API, traffic-aware for driving modes
    # ------------------------------------------------------------------
    def travel_time(
        self,
        origin: str,
        destination: str,
        mode: str = "drive",
        departure_time: str = "",
        avoid_tolls: bool = False,
        arrival_time: str = "",
        include_tolls: bool = False,
    ) -> str:
        travel_mode = self._resolve_mode(mode)
        body: dict = {
            "origin": self._waypoint(origin),
            "destination": self._waypoint(destination),
            "travelMode": travel_mode,
        }
        field_mask = _ROUTES_FIELD_MASK
        if travel_mode in _TRAFFIC_MODES:
            body["routingPreference"] = "TRAFFIC_AWARE"
            if avoid_tolls:
                body["routeModifiers"] = {"avoidTolls": True}
            if include_tolls:
                # Toll estimation is a separate (higher-SKU) computation —
                # only requested when explicitly asked for.
                body["extraComputations"] = ["TOLLS"]
                field_mask = _TOLLS_FIELD_MASK
        if arrival_time:
            if travel_mode != "TRANSIT":
                raise ValueError(
                    "arrival_time is only supported for mode='transit' "
                    "(the Routes API restriction)."
                )
            body["arrivalTime"] = arrival_time
        elif departure_time:
            body["departureTime"] = departure_time
        if travel_mode == "TRANSIT":
            field_mask = _TRANSIT_FIELD_MASK
        data = self._post(ROUTES_URL, body, field_mask)
        routes = data.get("routes", [])
        if not routes:
            return f"No route found from '{origin}' to '{destination}' ({mode})."
        r = routes[0]
        live_s = self._parse_duration_s(r.get("duration", ""))
        static_s = self._parse_duration_s(r.get("staticDuration", ""))
        km = r.get("distanceMeters", 0) / 1000
        lines = [
            f"{origin} → {destination} ({travel_mode.lower()}):",
            f"  duration: {self._format_minutes(live_s)}"
            + (f"  ({km:.1f} km)" if km else ""),
        ]
        if static_s and travel_mode in _TRAFFIC_MODES:
            delay = live_s - static_s
            if delay > 60:
                lines.append(
                    f"  traffic:  +{self._format_minutes(delay)} over the "
                    f"no-traffic baseline of {self._format_minutes(static_s)}"
                )
            else:
                lines.append("  traffic:  none to speak of")
        toll = (r.get("travelAdvisory") or {}).get("tollInfo") or {}
        for price in toll.get("estimatedPrice", []):
            units = int(price.get("units", 0))
            nanos = int(price.get("nanos", 0))
            lines.append(
                f"  tolls:    ~{units + nanos / 1e9:.2f} "
                f"{price.get('currencyCode', '')}".rstrip()
            )
        transit_steps = [
            st for leg in r.get("legs", []) for st in leg.get("steps", [])
            if st.get("transitDetails")
        ]
        for st in transit_steps:
            td = st["transitDetails"]
            line_info = td.get("transitLine", {})
            line_name = (
                line_info.get("nameShort") or line_info.get("name") or "?"
            )
            vehicle = (
                line_info.get("vehicle", {}).get("type", "").lower() or "ride"
            )
            stop_details = td.get("stopDetails", {})
            dep_stop = stop_details.get("departureStop", {}).get("name", "?")
            arr_stop = stop_details.get("arrivalStop", {}).get("name", "?")
            dep_time = self._clock(stop_details.get("departureTime", ""))
            arr_time = self._clock(stop_details.get("arrivalTime", ""))
            count = td.get("stopCount", "")
            stops = f", {count} stops" if count else ""
            lines.append(
                f"  transit:  {line_name} ({vehicle}) {dep_stop} {dep_time}"
                f" → {arr_stop} {arr_time}{stops}"
            )
        if arrival_time:
            lines.append(f"  arriving by: {arrival_time}")
        elif departure_time:
            lines.append(f"  departing: {departure_time}")
        return "\n".join(lines)

    @staticmethod
    def _clock(rfc3339: str) -> str:
        """Compact HH:MM from an RFC3339 timestamp ('' passthrough)."""
        if "T" not in rfc3339:
            return rfc3339
        return rfc3339.split("T", 1)[1][:5]

    # ------------------------------------------------------------------
    # time_zone
    # ------------------------------------------------------------------
    def time_zone(self, latitude: float, longitude: float, timestamp: int = 0) -> str:
        import time as _time
        data = self._get(TIMEZONE_URL, {
            "location": f"{latitude},{longitude}",
            "timestamp": timestamp or int(_time.time()),
        })
        raw = data.get("rawOffset", 0)
        dst = data.get("dstOffset", 0)
        total_h = (raw + dst) / 3600
        return (
            f"Time zone at {latitude},{longitude}:\n"
            f"  id:     {data.get('timeZoneId', '?')}\n"
            f"  name:   {data.get('timeZoneName', '?')}\n"
            f"  offset: UTC{'+' if total_h >= 0 else ''}{total_h:g}"
            + ("  (incl. DST)" if dst else "")
        )
