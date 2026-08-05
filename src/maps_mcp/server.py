import functools
import hmac
import inspect
import json
import logging
import time

import anyio
from mcp.server.fastmcp import FastMCP

from maps_mcp.config import MCP_BEARER_TOKEN, PORT
from maps_mcp.maps_client import MapsClient

logger = logging.getLogger(__name__)

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
client = MapsClient()


# --- Run blocking tool handlers off the event loop -----------------------
# The MCP SDK runs sync tools inline on the single event loop, so a blocking
# upstream HTTP call would stall every concurrent request. Each sync tool is
# registered as an async wrapper that runs the body in a worker thread; the
# original sync fn is returned so the module name stays directly callable
# for unit tests. httpx.Client is thread-safe, so no locking is needed.
req_logger = logging.getLogger("maps_mcp.requests")
if not req_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    req_logger.addHandler(_h)
    req_logger.setLevel(logging.INFO)
    req_logger.propagate = False


def _rss_mib() -> int:
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * 4096 // (1024 * 1024)
    except Exception:
        return -1


_orig_tool = mcp.tool


def _threaded_tool(*d_args, **d_kwargs):
    register = _orig_tool(*d_args, **d_kwargs)

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):
            return register(fn)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            outcome = "ok"
            try:
                return await anyio.to_thread.run_sync(
                    functools.partial(fn, *args, **kwargs)
                )
            except Exception:
                outcome = "error"
                raise
            finally:
                req_logger.info(
                    "tool=%s outcome=%s duration_ms=%d rss_mib=%d",
                    fn.__name__, outcome,
                    int((time.perf_counter() - start) * 1000),
                    _rss_mib(),
                )

        wrapper.__signature__ = inspect.signature(fn)
        register(wrapper)
        return fn

    return decorator


mcp.tool = _threaded_tool


class BearerAuthMiddleware:
    """Pure ASGI bearer-token middleware (streaming-safe, timing-safe compare)."""

    def __init__(self, app, expected_token: str):
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path.startswith("/.well-known/"):
                return await self.app(scope, receive, send)
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth.startswith("Bearer "):
                token = auth[7:]
                if hmac.compare_digest(token, self.expected_token):
                    return await self.app(scope, receive, send)
            client_host = scope.get("client", ("unknown",))[0]
            if client_host in ("127.0.0.1", "::1"):
                logger.debug("Health probe 401 (loopback, expected)")
            else:
                logger.warning(
                    "Rejected request: invalid or missing bearer token from %s",
                    client_host,
                )
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


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
    import sys

    logging.basicConfig(level=logging.INFO)

    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
        return

    import uvicorn

    if not MCP_BEARER_TOKEN:
        raise SystemExit(
            "MCP_BEARER_TOKEN is not set. Refusing to start an unauthenticated "
            "MCP server. Set it in .secrets.env and restart."
        )
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(), expected_token=MCP_BEARER_TOKEN,
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
