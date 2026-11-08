"""Configuration for the POI downloader. Edit here to add categories or change servers."""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Category -> list of OSM tags ("key=value"). Add your own categories here.
CATEGORY_MAPPING = {
    "airport": ["aeroway=aerodrome"],
    "hospital": ["amenity=hospital"],
    "school": ["amenity=school"],
    "university": ["amenity=university"],
    "restaurant": ["amenity=restaurant"],
    "hotel": ["tourism=hotel"],
    "bank": ["amenity=bank"],
    "fuel": ["amenity=fuel"],
    "pharmacy": ["amenity=pharmacy"],
    "shopping_mall": ["shop=mall"],
    # Extra categories / Indonesian aliases (also used by the web frontend)
    "supermarket": ["shop=supermarket"],
    "cafe": ["amenity=cafe"],
    "clinic": ["amenity=clinic"],
    "bus_station": ["amenity=bus_station"],
    "atm": ["amenity=atm"],
    "post_office": ["amenity=post_office"],
    "parking": ["amenity=parking"],
    "kindergarten": ["amenity=kindergarten"],
    "police": ["amenity=police"],
    "fire_station": ["amenity=fire_station"],
    "place_of_worship": ["amenity=place_of_worship"],
    "bandara": ["aeroway=aerodrome"],
    "rumah_sakit": ["amenity=hospital"],
    "sekolah": ["amenity=school"],
    "universitas": ["amenity=university"],
    "restoran": ["amenity=restaurant"],
    "spbu": ["amenity=fuel"],
    "apotek": ["amenity=pharmacy"],
    "mall": ["shop=mall"],
}

# Endpoints (change if a server is overloaded). Tried in order on failure.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

USER_AGENT = "POIDownloader/1.0 (OSM POI download tool; python-requests)"

DEFAULT_COUNTRY = "Indonesia"

# Writable runtime folders. The Vercel deployment filesystem is read-only except /tmp
# (and /tmp is NOT persistent), so output/logs/cache go to the system temp dir there.
# Locally they stay inside the project folder. Override with POI_DATA_DIR if needed.
IS_VERCEL = bool(os.environ.get("VERCEL"))
if os.environ.get("POI_DATA_DIR"):
    RUNTIME_DIR = Path(os.environ["POI_DATA_DIR"])
elif IS_VERCEL:
    RUNTIME_DIR = Path(tempfile.gettempdir()) / "poi_explorer"
else:
    RUNTIME_DIR = BASE_DIR
DEFAULT_OUTPUT_DIR = RUNTIME_DIR / "output"
LOG_DIR = RUNTIME_DIR / "logs"
CACHE_DIR = RUNTIME_DIR / ".cache"

# Network behaviour
REQUEST_TIMEOUT = 30            # seconds for Nominatim
OVERPASS_QUERY_TIMEOUT = 180    # server-side [timeout:] value
OVERPASS_HTTP_TIMEOUT = 240     # client-side read timeout
MAX_RETRIES = 3
RETRY_BACKOFF = 5               # seconds, multiplied by attempt number
REQUEST_DELAY = 2               # pause between requests (rate limiting)
NOMINATIM_MIN_INTERVAL = 1.1    # Nominatim policy: max 1 req/sec
TIME_BUDGET = None              # max seconds for one run_download (None = unlimited)

if IS_VERCEL:
    # Must finish inside vercel.json "maxDuration" (300 s), so use tighter limits.
    OVERPASS_QUERY_TIMEOUT = 110
    OVERPASS_HTTP_TIMEOUT = 125
    MAX_RETRIES = 2
    RETRY_BACKOFF = 3
    TIME_BUDGET = 270

# Polygon fallback: max points per ring sent to Overpass
POLY_MAX_POINTS = 400
