# maps-mcp

An [MCP](https://modelcontextprotocol.io) server exposing Google Maps
Platform to any MCP client: geocoding, place search/details,
traffic-aware travel times, and time zones. Six tools, built on the
[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
(FastMCP). Runs as a local stdio server or as a containerized
Streamable HTTP service with bearer auth.

Auth is a single API key, not OAuth — Maps Platform is a key-metered
developer API, so there are no accounts to connect and no token refresh.

## Tool Reference

| Tool | Parameters | Description |
|---|---|---|
| `geocode` | `address`, `region = ""` | Free-form address/place name → coordinates, canonical address, `place_id`. `region` is a ccTLD bias (e.g. `au`; default from `MAPS_REGION`). |
| `reverse_geocode` | `latitude`, `longitude` | Coordinates → nearest street address(es). |
| `place_search` | `query`, `latitude = 0`, `longitude = 0`, `radius_meters = 0`, `open_now = False`, `max_results = 5` | Text search for businesses/POIs ("vet near Potts Point"). Returns name, address, rating, open-now, phone, `place_id`. Optional circular location bias (default radius 5 km when a point is given). |
| `place_details` | `place_id` | One place in full: weekly opening hours, phone, website, rating, price level, editorial summary. |
| `travel_time` | `origin`, `destination`, `mode = "drive"`, `departure_time = ""`, `avoid_tolls = False`, `arrival_time = ""`, `include_tolls = False` | Route duration + distance via the Routes API; traffic-aware for `drive`/`two_wheeler` (reports delay vs no-traffic baseline). Transit answers include per-leg detail (line, stops, clock times) and accept `arrival_time` ("be there by") — transit only, per the API. `include_tolls` adds an estimated toll cost for driving modes (extra computation, off by default). `departure_time` RFC3339, now-or-future. |
| `place_search_nearby` | `latitude`, `longitude`, `included_types = ""`, `radius_meters = 1500`, `max_results = 5`, `rank_by_distance = False` | Typed "what's around me" (Places New searchNearby): `included_types` is a comma-separated place-type list (`pharmacy`, `restaurant,cafe`); optional nearest-first ranking. |
| `time_zone` | `latitude`, `longitude`, `timestamp = 0` | IANA zone + UTC offset (incl. DST) at a point; `timestamp` (epoch) evaluates DST at that moment. |

Origins/destinations for `travel_time` accept three spellings, resolved by
shape: a free-form address, `"lat,lng"`, or `"place_id:<id>"`.

```python
# "When do I need to leave?" — compose with your calendar MCP server
travel_time(
    origin="home address here",
    destination="325 Edgecliff Rd, Woollahra",   # from the event's location
    mode="drive",
    departure_time="2026-07-05T08:30:00+10:00",
)

# Find somewhere that's open right now
place_search(query="pharmacy Potts Point", open_now=True)
```

## Setup (Google Cloud console — one-time)

1. Create (or pick) a GCP project. **Prefer a dedicated project** — an API
   key is easier to leak than an OAuth token, and project isolation caps
   the blast radius. Enable billing (personal volumes sit inside the
   monthly free tiers, but the billing account is mandatory).
2. Enable four APIs: **Geocoding API**, **Places API (New)**, **Routes
   API**, **Time Zone API**.
3. Create an API key (Credentials → Create credentials → API key) and
   **restrict it** to exactly those four APIs. Add IP restrictions if the
   caller set is stable.
4. Set `MAPS_API_KEY` in the server's environment and restart. The server
   runs fine without the key — every tool call returns a setup-pointer
   error until it's set — so deployment order doesn't matter.

## Quick start (stdio)

Most MCP clients (Claude Code, Claude Desktop, VS Code, …) spawn stdio
servers directly. With [uv](https://docs.astral.sh/uv/) installed:

```jsonc
// e.g. Claude Desktop claude_desktop_config.json / Claude Code .mcp.json
{
  "mcpServers": {
    "maps": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/maps-mcp", "maps-mcp", "--stdio"],
      "env": { "MAPS_API_KEY": "your-key-here" }
    }
  }
}
```

stdio mode has no network surface and skips bearer auth — the client owns
the process.

## HTTP mode (container)

The bundled `Containerfile` builds a Streamable HTTP server at `/mcp`
(stateless — restarts never strand client sessions). HTTP mode **refuses
to start** without `MCP_BEARER_TOKEN`; clients authenticate with
`Authorization: Bearer <token>`.

```bash
podman build -t maps-mcp .    # or: docker build -t maps-mcp .
podman run -d --name maps-mcp -p 8328:8328 \
  -e MAPS_API_KEY=your-key -e MCP_BEARER_TOKEN=some-long-random-token \
  maps-mcp
```

`maps_mcp.healthcheck` does a full HTTP round-trip to `/mcp` (the 401
counts as alive — it proves the event loop responds); wire it to your
container healthcheck with a restart-on-unhealthy policy. Terminate TLS
at a reverse proxy — the server itself speaks plain HTTP.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MAPS_API_KEY` | *(empty)* | Google Maps Platform API key. Tools error clearly when unset. |
| `MAPS_REGION` | *(empty)* | Optional ccTLD geocoding bias (e.g. `au`). Empty lets Google decide. |
| `MAPS_LANGUAGE` | *(empty)* | Optional BCP-47 language for Places responses (e.g. `en-AU`). |
| `PORT` | `8328` | HTTP listen port. |
| `MCP_BEARER_TOKEN` | *(empty)* | Required in HTTP mode; server refuses to start without it. Not used in `--stdio` mode. |

## Architecture notes

- Four upstream APIs, one thread-safe `httpx.Client`. Legacy-style APIs
  (Geocoding, Time Zone) take the key as a query param and report errors
  in a body `status` field; new-style APIs (Places New, Routes) take
  `X-Goog-Api-Key` + a mandatory `X-Goog-FieldMask` header.
- Sync tool handlers are offloaded to a worker thread (the MCP SDK runs
  sync tools inline on the event loop, so a slow upstream call would
  otherwise stall every concurrent request). Per-request log lines
  (`tool= outcome= duration_ms= rss_mib=`) go to stderr.
- API-key values are redacted from error messages before they can reach
  logs or clients.

## Testing

```bash
# Tiers 1 + 2 — pure helpers + mocked HTTP (fast, no network, no key)
uv run --extra test pytest tests/test_maps_client.py -v

# Tier 3 — live API round-trips against stable Sydney landmarks
# (read-only; nothing to clean up). Gated on the key; skips without it.
MAPS_API_KEY=... uv run --extra test pytest tests/test_integration.py -v
```

## License

[MIT](LICENSE)
