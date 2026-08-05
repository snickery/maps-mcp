import os

# Google Maps Platform API key (NOT OAuth — Maps is a key-metered developer
# API). Created in the Google Cloud console with the Geocoding, Places (New),
# Routes, and Time Zone APIs enabled, ideally in a dedicated project so a
# leaked key can't touch anything else. The server starts fine without it —
# every tool call then returns a clear setup error instead, so the container
# and stdio registrations can be deployed before the key exists.
MAPS_API_KEY = os.environ.get("MAPS_API_KEY", "")

# Optional ccTLD region bias for geocoding (e.g. "au" ranks Australian
# matches first) and BCP-47 language for Places responses (e.g. "en-AU").
# Empty (the default) omits the parameter and lets Google decide.
MAPS_REGION = os.environ.get("MAPS_REGION", "")
MAPS_LANGUAGE = os.environ.get("MAPS_LANGUAGE", "")

PORT = int(os.environ.get("PORT", "8328"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
