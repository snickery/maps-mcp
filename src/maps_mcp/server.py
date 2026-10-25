import logging

from mcp.server.fastmcp import FastMCP
from mcp_server_kit import offload_sync_tools, serve

from maps_mcp.config import MCP_BEARER_TOKEN, PORT
from maps_mcp.maps_client import MapsClient

INSTRUCTIONS = """\
Google Maps Platform server: geocoding, place search/details, traffic-aware
travel times, and time zones. No accounts — one shared API key. Origins and
destinations for travel_time accept a free-form address, 'lat,lng', or
'place_id:<id>' (get place IDs from geocode / place_search). Compose with
a calendar MCP server for leave-by-time questions: event location →
travel_time(origin=home, destination=event, mode='drive')."""

mcp = FastMCP(
    "maps",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)
# Sync tools run in worker threads (a blocking Maps call would otherwise stall every
# request), and each call logs a tool=/outcome= line. httpx.Client is thread-safe.
offload_sync_tools(mcp, logger_name="maps_mcp.requests")
client = MapsClient()


@mcp.tool()
def geocode(address: str, region: str = "") -> str:
    """Resolve a free-form address or place name to coordinates, a
    canonical formatted address, and a place_id. region is a ccTLD bias
    (e.g. 'au'; default from the MAPS_REGION env var) — results elsewhere
    still resolve, just rank lower."""
    return client.geocode(address, region)


@mcp.tool()
def reverse_geocode(latitude: float, longitude: float) -> str:
    """Resolve coordinates to the nearest street address(es)."""
    return client.reverse_geocode(latitude, longitude)


@mcp.tool()
def place_search(
    query: str,
    latitude: float = 0.0,
    longitude: float = 0.0,
    radius_meters: int = 0,
    open_now: bool = False,
    max_results: int = 5,
) -> str:
    """Find businesses/points of interest by free-text query (e.g.
    'vet near Potts Point', 'best coffee Surry Hills'). Returns name,
    address, rating, open-now, phone, and place_id per result.

    latitude/longitude (+ optional radius_meters, default 5 km) bias
    results toward a point — or just put the suburb in the query.
    open_now=True filters to places currently open."""
    return client.place_search(
        query, latitude, longitude, radius_meters, open_now, max_results,
    )


@mcp.tool()
def place_details(place_id: str) -> str:
    """Full detail for one place by place_id (from place_search or
    geocode): weekly opening hours, phone, website, rating, price level,
    editorial summary."""
    return client.place_details(place_id)


@mcp.tool()
def travel_time(
    origin: str,
    destination: str,
    mode: str = "drive",
    departure_time: str = "",
    avoid_tolls: bool = False,
    arrival_time: str = "",
    include_tolls: bool = False,
) -> str:
    """Route duration and distance between two points — traffic-aware
    for driving, step-by-step for transit. THE tool for "when do I need
    to leave?".

    origin/destination: address, 'lat,lng', or 'place_id:<id>'.
    mode: drive (default) / walk / bicycle / transit / two_wheeler.
    departure_time: RFC3339 (e.g. '2026-07-05T08:30:00+10:00'), must be
      now-or-future; empty means now. For driving, the result includes
      the delay vs the no-traffic baseline.
    arrival_time: RFC3339 "be there by" time — transit mode only (the
      Routes API restriction). The transit answer lists each leg: line,
      stops, and clock times, so "leave by" falls straight out.
    include_tolls: for drive/two_wheeler, adds an estimated toll cost
      (requests an extra computation — off by default)."""
    return client.travel_time(
        origin, destination, mode, departure_time, avoid_tolls,
        arrival_time, include_tolls,
    )


@mcp.tool()
def place_search_nearby(
    latitude: float,
    longitude: float,
    included_types: str = "",
    radius_meters: int = 1500,
    max_results: int = 5,
    rank_by_distance: bool = False,
) -> str:
    """Find places around a point by TYPE ('what's near me') — the typed
    complement to place_search's free-text queries.

    latitude/longitude: center of the search circle.
    included_types: comma-separated place types, e.g. 'pharmacy',
      'restaurant,cafe', 'ev_charging_station' (Places API type table);
      empty returns prominent places of any type.
    radius_meters: search radius (default 1500, max 50000).
    max_results: max places (default 5, max 20).
    rank_by_distance: True ranks nearest-first instead of by prominence.
    """
    return client.place_search_nearby(
        latitude, longitude, included_types, radius_meters,
        max_results, rank_by_distance,
    )


@mcp.tool()
def time_zone(latitude: float, longitude: float, timestamp: int = 0) -> str:
    """IANA time zone + UTC offset (incl. DST) at coordinates. timestamp
    (unix epoch seconds) evaluates DST at a specific moment; 0 = now."""
    return client.time_zone(latitude, longitude, timestamp)


def main():
    logging.basicConfig(level=logging.INFO)
    serve(mcp, port=PORT, bearer_token=MCP_BEARER_TOKEN)


if __name__ == "__main__":
    main()
