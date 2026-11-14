
"""ERAU ASME Combat Robotics Event Tracker.

Automatically discovers upcoming combat robotics competitions,
filters by distance from Embry-Riddle Daytona Beach, and sends
Discord notifications.

Features:
- Robot Combat Events, BuildersDB, NHRL, Robot Ruckus
- Manually curated events
- Automatic US city geocoding
- Persistent location caching
- Cross-source event deduplication
- Duplicate notification prevention
- Registration update notifications
- Weekly Discord summaries
- Source health diagnostics

Dependencies:
    requests
    beautifulsoup4
    python-dateutil

Required GitHub Actions secret:
    DISCORD_WEBHOOK_URL
"""

import hashlib
import json
import math
import os
import re

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser


# ============================================================
# CONFIGURATION
# ============================================================

HOME_NAME = "ERAU Daytona Beach"
HOME_LAT = 29.1900
HOME_LON = -81.0480

RADIUS_MILES = float(
    os.getenv("RADIUS_MILES", "450")
)

MAJOR_RADIUS_MILES = float(
    os.getenv("MAJOR_EVENT_RADIUS_MILES", "1100")
)

WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_URL", ""
)

ROLE_MENTION = os.getenv(
    "ROLE_MENTION", ""
).strip()

POST_NO_EVENT_MESSAGE = (
    os.getenv(
        "POST_NO_EVENT_MESSAGE", "false"
    ).lower() == "true"
)

FORCE_SUMMARY = (
    os.getenv(
        "FORCE_SUMMARY", "false"
    ).lower() == "true"
)

MAX_GEOCODE_REQUESTS = 12
MAX_NOTIFICATIONS = 10

STATE_PATH = Path("tracker_state.json")
OLD_STATE_PATH = Path("posted_events.json")
SNAPSHOT_PATH = Path("latest_events.json")
STATUS_PATH = Path("source_status.json")
CURATED_PATH = Path("curated_events.json")

TODAY = datetime.now(
    ZoneInfo("America/New_York")
).date()


# ============================================================
# SOURCES
# ============================================================

SOURCES = [
    (
        "Robot Combat Events",
        "https://www.robotcombatevents.com/events/",
        "rce",
        False,
    ),
    (
        "BuildersDB",
        "https://www.buildersdb.com/events?show=all",
        "buildersdb",
        False,
    ),
    (
        "NHRL",
        "https://www.nhrl.io/events",
        "nhrl",
        True,
    ),
    (
        "Robot Ruckus",
        "https://www.robotruckus.org/",
        "ruckus",
        True,
    ),
]

SOURCE_PRIORITY = {
    "Robot Combat Events": 5,
    "BuildersDB": 4,
    "NHRL": 4,
    "Robot Ruckus": 4,
    "Curated": 2,
}


# ============================================================
# US STATES
# ============================================================

US_STATES = dict(zip(
    (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA "
        "KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
        "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT "
        "VA WA WV WI WY"
    ).split(),
    (
        "Alabama|Alaska|Arizona|Arkansas|California|"
        "Colorado|Connecticut|Delaware|Florida|Georgia|"
        "Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|"
        "Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
        "Michigan|Minnesota|Mississippi|Missouri|Montana|"
        "Nebraska|Nevada|New Hampshire|New Jersey|"
        "New Mexico|New York|North Carolina|North Dakota|"
        "Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
        "South Carolina|South Dakota|Tennessee|Texas|"
        "Utah|Vermont|Virginia|Washington|West Virginia|"
        "Wisconsin|Wyoming"
    ).split("|")
))


# ============================================================
# KNOWN CITY COORDINATES
# ============================================================

# These cities do not require an API request.
# Other US cities are looked up automatically.

CITIES = {
    "daytona beach, fl": (29.2108, -81.0228),
    "orlando, fl": (28.5383, -81.3792),
    "ocala, fl": (29.1872, -82.1401),
    "jacksonville, fl": (30.3322, -81.6557),
    "fort pierce, fl": (27.4467, -80.3256),
    "west palm beach, fl": (26.7153, -80.0534),
    "tampa, fl": (27.9506, -82.4572),
    "kissimmee, fl": (28.2919, -81.4076),
    "miami, fl": (25.7617, -80.1918),
    "fort lauderdale, fl": (26.1224, -80.1378),
    "gainesville, fl": (29.6516, -82.3248),
    "tallahassee, fl": (30.4383, -84.2807),
    "pensacola, fl": (30.4213, -87.2169),
    "st. petersburg, fl": (27.7676, -82.6403),
    "lakeland, fl": (28.0395, -81.9498),
    "melbourne, fl": (28.0836, -80.6081),
    "fort myers, fl": (26.6406, -81.8723),
    "sarasota, fl": (27.3364, -82.5307),

    "atlanta, ga": (33.7490, -84.3880),
    "savannah, ga": (32.0809, -81.0912),
    "augusta, ga": (33.4735, -82.0105),
    "columbus, ga": (32.4610, -84.9877),

    "columbia, sc": (34.0007, -81.0348),
    "charleston, sc": (32.7765, -79.9311),
    "greenville, sc": (34.8526, -82.3940),

    "charlotte, nc": (35.2271, -80.8431),
    "winston-salem, nc": (36.0999, -80.2442),
    "raleigh, nc": (35.7796, -78.6382),
    "hickory, nc": (35.7345, -81.3445),
    "asheville, nc": (35.5951, -82.5515),

    "birmingham, al": (33.5186, -86.8104),
    "mobile, al": (30.6954, -88.0399),
    "huntsville, al": (34.7304, -86.5861),

    "nashville, tn": (36.1627, -86.7816),
    "chattanooga, tn": (35.0456, -85.3097),

    "norwalk, ct": (41.1176, -73.4079),
    "south norwalk, ct": (41.0959, -73.4176),
}


# ============================================================
# REGULAR EXPRESSIONS
# ============================================================

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)

DATE_REGEX = re.compile(
    rf"\b{MONTH}\s+\d{{1,2}}"
    rf"(?:,?\s+20\d{{2}})?|"
    rf"\b20\d{{2}}-\d{{2}}-\d{{2}}",
    re.I,
)

LOCATION_REGEX = re.compile(
    r"([A-Za-z][A-Za-z .'-]{1,65}),\s*"
    r"([A-Z]{2}|"
    + "|".join(
        re.escape(s)
        for s in sorted(
            US_STATES.values(),
            key=len,
            reverse=True,
        )
    )
    + r")\b",
    re.I,
)

WEIGHTS_REGEX = re.compile(
    r"\b(?:150\s*g|1\s*lb|3\s*lb|12\s*lb|"
    r"15\s*lb|30\s*lb|60\s*lb|120\s*lb|"
    r"220\s*lb|Fairyweight|Antweight|"
    r"Plastic Ant|Beetleweight|Hobbyweight|"
    r"Featherweight|Sportsman)\b",
    re.I,
)

REG_REGEX = re.compile(
    r"Registration\s+"
    r"(?:closed|open|opens?\s+(?:on\s+)?"
    r"[A-Za-z]+\s+\d{1,2},?\s+20\d{2})",
    re.I,
)


# ============================================================
# EVENT MODEL
# ============================================================

@dataclass
class Event:
    title: str
    date: str
    location: str
    url: str
    source: str

    classes: str = "Not specified"
    registration: str = "Check event page"
    major: bool = False
    distance_miles: float | None = None

    @property
    def event_id(self):
        # Preserve compatibility with earlier tracker IDs.
        canonical = (
            self.source.lower(),
            self.url.rstrip("/"),
            self.title.lower(),
            self.date,
        )

        return hashlib.sha256(
            json.dumps(canonical).encode()
        ).hexdigest()[:16]


# ============================================================
# DATE UTILITIES
# ============================================================

def parse_day(value):
    if not value:
        return None

    try:
        return dtparser.parse(
            str(value),
            fuzzy=True,
            default=datetime(TODAY.year, 1, 1),
        ).date()

    except (
        ValueError,
        TypeError,
        OverflowError,
    ):
        return None


def find_date(text):
    match = DATE_REGEX.search(text)

    if not match:
        return None

    snippet = match.group(0)

    if not re.search(r"20\d{2}", snippet):
        nearby = text[
            match.start():match.start() + 110
        ]

        year = re.search(
            r"20\d{2}",
            nearby,
        )

        if not year:
            return None

        snippet += ", " + year.group(0)

    day = parse_day(snippet)

    return day.isoformat() if day else None


# ============================================================
# LOCATION NORMALIZATION
# ============================================================

def normalize_location(location):
    text = re.sub(
        r"\s+",
        " ",
        str(location),
    ).strip()

    text = re.sub(
        r",?\s*(?:United States|USA|US)\s*$",
        "",
        text,
        flags=re.I,
    ).strip(" ,")

    match = re.fullmatch(
        r"([A-Za-z][A-Za-z .'-]+),\s*"
        r"([A-Za-z ]+)",
        text,
        re.I,
    )

    if not match:
        return None

    city = match.group(1).strip()
    region = match.group(2).strip()

    if len(region) == 2:
        state = region.upper()

    else:
        state = next(
            (
                code
                for code, name in US_STATES.items()
                if name.casefold()
                == region.casefold()
            ),
            None,
        )

    if state not in US_STATES:
        return None

    return f"{city.title()}, {state}"


def location_from(text):
    matches = list(
        LOCATION_REGEX.finditer(text)
    )

    if not matches:
        return ""

    # Accept the last location inside this event's own text.
    # The caller must not supply text containing other events.
    location = normalize_location(
        matches[-1].group(0)
    )

    return location or ""


# ============================================================
# AUTOMATIC CITY GEOCODING
# ============================================================

def coordinates_for(location, state, session):
    key = location.casefold()

    if key in CITIES:
        return CITIES[key]

    cache = state.setdefault(
        "geocode_cache",
        {},
    )

    if key in cache:
        coords = cache[key]
        return tuple(coords) if coords else None

    attempted = state.setdefault(
        "_geocode_attempted",
        [],
    )

    if key in attempted:
        return None

    if len(attempted) >= MAX_GEOCODE_REQUESTS:
        print(
            f"[GEO] Limit reached: {location}"
        )
        return None

    attempted.append(key)

    city, region = location.rsplit(
        ", ",
        1,
    )

    try:
        response = session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city,
                "count": 30,
                "language": "en",
                "countryCode": "US",
            },
            timeout=15,
        )

        response.raise_for_status()

        results = response.json().get(
            "results",
            [],
        )

        matches = [
            item
            for item in results
            if (
                item.get(
                    "name", ""
                ).casefold() == city.casefold()
                and item.get(
                    "admin1", ""
                ).casefold()
                == US_STATES[region].casefold()
                and item.get(
                    "country_code"
                ) == "US"
            )
        ]

        if not matches:
            print(
                f"[GEO] {location}: no verified match"
            )
            return None

        result = matches[0]

        coords = [
            float(result["latitude"]),
            float(result["longitude"]),
        ]

        cache[key] = coords

        print(
            f"[GEO] {location}: resolved"
        )

        return tuple(coords)

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            f"[GEO] {location}: "
            f"lookup failed ({type(exc).__name__})"
        )

        return None


# ============================================================
# DISTANCE
# ============================================================

def distance(lat, lon):
    a = math.radians(HOME_LAT)
    b = math.radians(lat)

    dp = b - a
    dl = math.radians(lon - HOME_LON)

    x = (
        math.sin(dp / 2) ** 2
        + math.cos(a)
        * math.cos(b)
        * math.sin(dl / 2) ** 2
    )

    return (
        3958.7613 * 2
        * math.atan2(
            math.sqrt(x),
            math.sqrt(1 - x),
        )
    )


# ============================================================
# EVENT DETAIL EXTRACTION
# ============================================================

def details_from(text):
    classes = []

    for match in WEIGHTS_REGEX.finditer(text):
        item = match.group(0).strip()

        if item.casefold() not in [
            x.casefold() for x in classes
        ]:
            classes.append(item)

    registration_match = REG_REGEX.search(
        text
    )

    registration = (
        registration_match.group(0).strip()
        if registration_match
        else "Check event page"
    )

    return (
        ", ".join(classes[:8])
        if classes
        else "Not specified",
        registration,
    )


# ============================================================
# WEBSITE REQUESTS
# ============================================================

def fetch(session, url):
    response = session.get(
        url,
        timeout=25,
    )

    response.raise_for_status()

    return response.text


# ============================================================
# ROBOT COMBAT EVENTS
# ============================================================

def parse_rce_card(text):
    """
    Extract an event from one isolated listing.

    Do not search shared containers containing unrelated
    competitions. This prevents mixing locations and dates.
    """

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    match = re.search(
        rf"\b({MONTH})\s+(\d{{1,2}})"
        rf"(?:\s*[–-]\s*(?:{MONTH}\s+)?\d{{1,2}})?"
        rf",?\s+(20\d{{2}})\b",
        text,
        re.I,
    )

    if not match:
        return None

    title = text[
        :match.start()
    ].strip(" -–|")

    if not 3 <= len(title) <= 170:
        return None

    date_text = (
        f"{match.group(1)} "
        f"{match.group(2)}, "
        f"{match.group(3)}"
    )

    day = parse_day(date_text)

    if not day:
        return None

    tail = text[
        match.end():
    ].strip()

    # Match the city/state directly after the event date.
    location_match = LOCATION_REGEX.match(
        tail
    )

    if not location_match:
        return None

    location = normalize_location(
        location_match.group(0)
    )

    if not location:
        return None

    details = tail[
        location_match.end():
    ]

    classes, registration = details_from(
        details
    )

    return (
        title,
        day.isoformat(),
        location,
        classes,
        registration,
    )


def parse_jsonld_events(soup, source, major=False):
    events = []

    def explore(value):
        if isinstance(value, list):
            for item in value:
                explore(item)
            return

        if not isinstance(value, dict):
            return

        typ = value.get("@type", "")

        is_event = (
            typ == "Event"
            or (
                isinstance(typ, list)
                and "Event" in typ
            )
        )

        if is_event:
            title = value.get("name", "")
            day = parse_day(
                value.get("startDate", "")
            )

            loc = value.get(
                "location",
                {},
            )

            if isinstance(loc, dict):
                address = loc.get(
                    "address",
                    {},
                )

                if isinstance(address, dict):
                    loc = ", ".join(
                        str(x)
                        for x in [
                            address.get(
                                "addressLocality", ""
                            ),
                            address.get(
                                "addressRegion", ""
                            ),
                        ]
                        if x
                    )
                else:
                    loc = str(
                        address or loc.get("name", "")
                    )

            location = normalize_location(
                str(loc)
            )

            if title and day and location:
                classes, registration = details_from(
                    json.dumps(value)
                )

                events.append(
                    Event(
                        title=str(title),
                        date=day.isoformat(),
                        location=location,
                        url=str(
                            value.get("url", "")
                        ),
                        source=source,
                        classes=classes,
                        registration=registration,
                        major=major,
                    )
                )

        for item in value.values():
            if isinstance(item, (list, dict)):
                explore(item)

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        try:
            data = json.loads(
                script.string
                or script.get_text()
            )
            explore(data)

        except (
            TypeError,
            ValueError,
        ):
            continue

    return events


def parse_rce(html, base):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    found = parse_jsonld_events(
        soup,
        "Robot Combat Events",
    )

    seen = {
        event.url.rstrip("/")
        for event in found
        if event.url
    }

    for link in soup.select("a[href]"):
        href = link.get(
            "href",
            "",
        )

        if not re.search(
            r"/events/\d+(?:/|\?|$)",
            href,
        ):
            continue

        url = urljoin(
            base,
            href,
        )

        if url.rstrip("/") in seen:
            continue

        record = parse_rce_card(
            link.get_text(
                " ",
                strip=True,
            )
        )

        if record is None:
            ancestor = link.parent

            for _ in range(4):
                if ancestor is None:
                    break

                related = [
                    a
                    for a in ancestor.select(
                        "a[href]"
                    )
                    if re.search(
                        r"/events/\d+(?:/|\?|$)",
                        a.get("href", ""),
                    )
                ]

                # Do not read a shared container.
                if len(related) != 1:
                    break

                text = ancestor.get_text(
                    " ",
                    strip=True,
                )

                if len(text) > 650:
                    break

                if len(
                    DATE_REGEX.findall(text)
                ) == 1:
                    record = parse_rce_card(
                        text
                    )

                    if record:
                        break

                ancestor = ancestor.parent

        if record is None:
            continue

        (
            title,
            day,
            location,
            classes,
            registration,
        ) = record

        found.append(
            Event(
                title=title,
                date=day,
                location=location,
                url=url,
                source="Robot Combat Events",
                classes=classes,
                registration=registration,
            )
        )

        seen.add(
            url.rstrip("/")
        )

    return found


def get_rce_events(session):
    urls = [
        "https://www.robotcombatevents.com/events/",
        "https://www.robotcombatevents.com/event_map",
    ]

    diagnostics = []

    for url in urls:
        try:
            html = fetch(
                session,
                url,
            )

            events = parse_rce(
                html,
                url,
            )

            diagnostics.append(
                f"{url}: HTTP 200; "
                f"extracted {len(events)} events"
            )

            if events:
                return (
                    events,
                    "ok",
                    "; ".join(diagnostics),
                )

        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )

            diagnostics.append(
                f"{url}: HTTP {status}"
            )

        except requests.RequestException as exc:
            diagnostics.append(
                f"{url}: request error "
                f"({type(exc).__name__})"
            )

        except Exception as exc:
            diagnostics.append(
                f"{url}: parser error "
                f"({type(exc).__name__})"
            )

    blocked = (
        diagnostics
        and all(
            "HTTP 403" in item
            or "HTTP 429" in item
            for item in diagnostics
        )
    )

    return (
        [],
        "blocked/unavailable"
        if blocked
        else "zero detected",
        "; ".join(diagnostics),
    )


# ============================================================
# BUILDERSDB
# ============================================================

def parse_buildersdb(html):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    result = []

    for link in soup.select(
        "a[href]"
    ):
        text = link.get_text(
            " ",
            strip=True,
        )

        if (
            len(text) < 25
            or len(text) > 800
            or "Registration:" not in text
        ):
            continue

        day = find_date(text)

        # Match the event city independently.
        location = location_from(text)

        if not day or not location:
            continue

        title = text

        # Strip trailing metadata when present.
        match = re.search(
            rf"\b{MONTH}\s+\d{{1,2}}",
            title,
            re.I,
        )

        if match:
            title = title[
                :match.start()
            ].strip(" -–|")

        if not title:
            title = text[:75]

        classes, registration = details_from(
            text
        )

        result.append(
            Event(
                title=title,
                date=day,
                location=location,
                url=urljoin(
                    "https://www.buildersdb.com",
                    link.get("href", ""),
                ),
                source="BuildersDB",
                classes=classes,
                registration=registration,
            )
        )

    return result


# ============================================================
# NHRL
# ============================================================

def parse_nhrl(html, base):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    result = parse_jsonld_events(
        soup,
        "NHRL",
        True,
    )

    for heading in soup.find_all(
        ["h2", "h3", "h4", "h5"]
    ):
        title = heading.get_text(
            " ",
            strip=True,
        )

        if not any(
            word in title.lower()
            for word in [
                "championship",
                "qualifier",
            ]
        ):
            continue

        parent = heading.parent

        if not parent:
            continue

        text = (
            parent.parent.get_text(
                " ",
                strip=True,
            )[:700]
            if parent.parent
            else parent.get_text(
                " ",
                strip=True,
            )
        )

        day = find_date(text)

        location = location_from(
            text.replace(
                "Norwalk CT",
                "Norwalk, CT",
            )
        )

        if day and location:
            result.append(
                Event(
                    title=title,
                    date=day,
                    location=location,
                    url=base,
                    source="NHRL",
                    classes="3 lb, 12 lb, 30 lb",
                    registration="Check event page",
                    major=True,
                )
            )

    return result


# ============================================================
# ROBOT RUCKUS
# ============================================================

def parse_ruckus(html, base):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    result = parse_jsonld_events(
        soup,
        "Robot Ruckus",
        True,
    )

    text = soup.get_text(
        " ",
        strip=True,
    )[:5000]

    match = re.search(
        rf"({MONTH})\s+(\d{{1,2}})"
        rf"(?:\s*&\s*\d{{1,2}})?,?\s+"
        rf"(20\d{{2}})",
        text,
        re.I,
    )

    if match:
        day = parse_day(
            f"{match.group(1)} "
            f"{match.group(2)}, "
            f"{match.group(3)}"
        )

        if day:
            result.append(
                Event(
                    title=(
                        "Robot Ruckus "
                        + match.group(3)
                    ),
                    date=day.isoformat(),
                    location="Orlando, FL",
                    url=base,
                    source="Robot Ruckus",
                    classes="Check event page",
                    registration="Check event page",
                    major=True,
                )
            )

    return result


# ============================================================
# CURATED EVENTS
# ============================================================

def load_curated():
    if not CURATED_PATH.exists():
        return []

    try:
        data = json.loads(
            CURATED_PATH.read_text(
                encoding="utf8"
            )
        )

        return [
            Event(
                title=str(
                    item["title"]
                ),
                date=str(
                    item["date"]
                ),
                location=str(
                    item["location"]
                ),
                url=str(
                    item["url"]
                ),
                source="Curated",
                classes=str(
                    item.get(
                        "classes",
                        "Not specified",
                    )
                ),
                registration=str(
                    item.get(
                        "registration",
                        "Check event page",
                    )
                ),
                major=bool(
                    item.get(
                        "major",
                        False,
                    )
                ),
            )
            for item in data
        ]

    except (
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        raise RuntimeError(
            f"Invalid curated_events.json: {exc}"
        ) from exc


# ============================================================
# DUPLICATE EVENT DETECTION
# ============================================================

def normalized_title(event):
    text = event.title.casefold()

    location = normalize_location(
        event.location
    )

    if location:
        city, state = location.rsplit(
            ", ",
            1,
        )

        state_name = US_STATES[state]

        # Remove repeated location text from titles.
        for suffix in [
            f"{city}, {state}",
            f"{city}, {state_name}",
            f"{city}, {state}, united states",
            f"{city}, {state_name}, united states",
        ]:
            if text.endswith(suffix.casefold()):
                text = text[
                    :-len(suffix)
                ].strip(" -–|,")

    # Remove common non-title metadata if embedded
    # in an event name.
    text = re.sub(
        r"\bregistration\s+"
        r"(?:open|closed|opens).*",
        "",
        text,
        flags=re.I,
    )

    # Remove a full trailing event date.
    text = re.sub(
        rf"\b{MONTH}\s+\d{{1,2}},?\s+20\d{{2}}$",
        "",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text,
    ).strip()

    return text


def same_competition(a, b):
    if a.date != b.date:
        return False

    loc_a = normalize_location(
        a.location
    )

    loc_b = normalize_location(
        b.location
    )

    if loc_a != loc_b:
        return False

    title_a = normalized_title(a)
    title_b = normalized_title(b)

    if not title_a or not title_b:
        return False

    if title_a == title_b:
        return True

    similarity = SequenceMatcher(
        None,
        title_a,
        title_b,
    ).ratio()

    return similarity >= 0.88


def deduplicate(events):
    unique = []
    duplicates = 0

    for event in events:
        counterpart = next(
            (
                previous
                for previous in unique
                if same_competition(
                    previous,
                    event,
                )
            ),
            None,
        )

        if counterpart is None:
            event._aliases = {
                event.event_id
            }

            unique.append(event)
            continue

        duplicates += 1

        aliases = (
            counterpart._aliases
            | {event.event_id}
        )

        old_priority = SOURCE_PRIORITY.get(
            counterpart.source,
            0,
        )

        new_priority = SOURCE_PRIORITY.get(
            event.source,
            0,
        )

        if new_priority > old_priority:
            event._aliases = aliases

            index = unique.index(
                counterpart
            )

            unique[index] = event

        else:
            counterpart._aliases = aliases

    print(
        f"[DEDUP] Removed {duplicates} "
        f"duplicate event records"
    )

    return unique


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    if STATE_PATH.exists():
        state = json.loads(
            STATE_PATH.read_text(
                encoding="utf8"
            )
        )

        state.setdefault(
            "announced",
            {},
        )

        state.setdefault(
            "geocode_cache",
            {},
        )

        state.setdefault(
            "last_summary",
            "",
        )

        state.setdefault(
            "last_warning",
            "",
        )

        return state

    old_ids = []

    if OLD_STATE_PATH.exists():
        old_ids = json.loads(
            OLD_STATE_PATH.read_text(
                encoding="utf8"
            )
        )

    return {
        "announced": {
            str(item): {
                "registration": "",
            }
            for item in old_ids
        },
        "geocode_cache": {},
        "last_summary": "",
        "last_warning": "",
    }


# ============================================================
# DISCORD
# ============================================================

def send(payload):
    if not WEBHOOK:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL secret is missing"
        )

    response = requests.post(
        WEBHOOK,
        json=payload,
        timeout=25,
    )

    response.raise_for_status()


def embed(event, update=False):
    if event.distance_miles is not None:
        distance_text = (
            f"{event.distance_miles:.0f} miles "
            "straight-line from ERAU"
        )
    else:
        distance_text = "Unknown"

    title = (
        "Event updated: "
        if update
        else "New event: "
    ) + event.title

    return {
        "title": title[:256],
        "url": event.url or None,
        "color": (
            0xF2AA4C
            if update
            else 0x2878CF
        ),
        "fields": [
            {
                "name": "Date",
                "value": event.date,
                "inline": True,
            },
            {
                "name": "Location",
                "value": event.location,
                "inline": True,
            },
            {
                "name": "Distance",
                "value": distance_text,
                "inline": True,
            },
            {
                "name": "Weight classes",
                "value": event.classes[:900],
                "inline": False,
            },
            {
                "name": "Registration",
                "value": event.registration[:250],
                "inline": True,
            },
            {
                "name": "Source",
                "value": event.source,
                "inline": True,
            },
        ],
        "footer": {
            "text": (
                "Verify dates and registration "
                "with the event organizer"
            )
        },
    }


# ============================================================
# COLLECT EVENTS
# ============================================================

def collect(state):
    session = requests.Session()

    session.headers["User-Agent"] = (
        "Mozilla/5.0 "
        "(compatible; ERAUCombatEventTracker/3.0; "
        "event monitoring)"
    )

    sources = []
    raw = []

    for name, url, kind, major in SOURCES:
        if kind == "rce":
            found, status, detail = get_rce_events(
                session
            )

            sources.append({
                "name": name,
                "url": url,
                "parsed": len(found),
                "status": status,
                "detail": detail,
            })

            print(
                f"[SOURCE] {name}: {len(found)} "
                f"events extracted ({status})"
            )

            print(
                f"[SOURCE DETAIL] {detail}"
            )

            raw.extend(found)
            continue

        try:
            html = fetch(
                session,
                url,
            )

            parsers = {
                "buildersdb": lambda: parse_buildersdb(
                    html
                ),
                "nhrl": lambda: parse_nhrl(
                    html,
                    url,
                ),
                "ruckus": lambda: parse_ruckus(
                    html,
                    url,
                ),
            }

            found = parsers[kind]()

            sources.append({
                "name": name,
                "url": url,
                "parsed": len(found),
                "status": (
                    "ok"
                    if found
                    else "zero detected"
                ),
            })

            print(
                f"[SOURCE] {name}: "
                f"{len(found)} events extracted"
            )

            raw.extend(found)

        except Exception as exc:
            sources.append({
                "name": name,
                "url": url,
                "parsed": 0,
                "status": "error",
                "detail": str(exc)[:250],
            })

            print(
                f"[SOURCE ERROR] {name}: {exc}"
            )

    curated = load_curated()
    raw.extend(curated)

    print(
        f"[SOURCE] Curated: "
        f"{len(curated)} events loaded"
    )

    skipped = {
        "old": 0,
        "unknown_date": 0,
        "unknown_location": 0,
        "out_of_radius": 0,
    }

    eligible = []

    for event in raw:
        day = parse_day(
            event.date
        )

        if not day:
            skipped["unknown_date"] += 1
            continue

        if day < TODAY:
            skipped["old"] += 1
            continue

        event.date = day.isoformat()

        location = normalize_location(
            event.location
        )

        if location is None:
            skipped["unknown_location"] += 1
            continue

        event.location = location

        coords = coordinates_for(
            location,
            state,
            session,
        )

        if coords is None:
            skipped["unknown_location"] += 1
            continue

        event.distance_miles = round(
            distance(*coords),
            1,
        )

        radius = (
            MAJOR_RADIUS_MILES
            if event.major
            else RADIUS_MILES
        )

        if event.distance_miles > radius:
            skipped["out_of_radius"] += 1
            continue

        eligible.append(event)

    unique = deduplicate(
        eligible
    )

    unique.sort(
        key=lambda event: (
            event.date,
            event.distance_miles,
            event.title,
        )
    )

    print(
        f"[FILTER] {len(raw)} extracted total; "
        f"{len(unique)} eligible; "
        f"skipped={skipped}"
    )

    return unique, sources, skipped


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()

    events, sources, skipped = collect(
        state
    )

    # Don't store temporary request counters.
    state.pop(
        "_geocode_attempted",
        None,
    )

    announced = state["announced"]

    known = {}

    for event in events:
        previous = next(
            (
                announced[alias]
                for alias in sorted(
                    event._aliases
                )
                if alias in announced
            ),
            None,
        )

        known[event.event_id] = previous

    new_events = [
        event
        for event in events
        if known[event.event_id] is None
    ]

    updated_events = [
        event
        for event in events
        if (
            known[event.event_id] is not None
            and known[event.event_id].get(
                "registration", ""
            )
            and known[event.event_id]["registration"]
            != event.registration
        )
    ]

    print(
        f"[RESULT] {len(events)} nearby events, "
        f"{len(new_events)} new, "
        f"{len(updated_events)} registration updates"
    )

    for event in events:
        print(
            f"  {event.date}: {event.title} | "
            f"{event.location} | "
            f"{event.distance_miles:.0f} mi | "
            f"{event.source}"
        )

    notifications = 0

    # New event and registration change notifications.
    for event in (
        new_events + updated_events
    )[:MAX_NOTIFICATIONS]:

        is_update = event in updated_events

        content = (
            "Registration information changed"
            if is_update
            else "New nearby combat robotics event"
        )

        if ROLE_MENTION:
            content = (
                ROLE_MENTION
                + "\n"
                + content
            )

        send({
            "username": "Combat Event Tracker",
            "content": content,
            "embeds": [
                embed(
                    event,
                    is_update,
                )
            ],
        })

        # Mark all equivalent IDs announced only after
        # Discord confirms delivery.
        for alias in event._aliases:
            announced[alias] = {
                "registration": event.registration,
                "date": event.date,
            }

        notifications += 1

    # Save equivalent source IDs for events that were
    # already announced, without reposting them.
    for event in events:
        if (
            event in new_events
            or event in updated_events
        ):
            continue

        previous = known[
            event.event_id
        ]

        if previous is not None:
            for alias in event._aliases:
                announced[alias] = previous

    local_date = TODAY.isoformat()

    # ========================================================
    # WEEKLY SUMMARY
    # ========================================================

    should_summarize = (
        TODAY.weekday() == 0
        or FORCE_SUMMARY
    )

    if (
        should_summarize
        and state.get(
            "last_summary"
        ) != local_date
    ):
        summary = sorted(
            events,
            key=lambda event: (
                event.date,
                event.distance_miles,
            ),
        )[:10]

        if summary:
            lines = [
                (
                    f"**{event.date}** | "
                    f"{event.title} | "
                    f"{event.location} | "
                    f"{event.distance_miles:.0f} "
                    "mi straight-line"
                )
                for event in summary
            ]

            message = (
                "**Upcoming Combat Robotics Events "
                "Near ERAU**\n"
                + "\n".join(lines)
            )

        else:
            message = (
                "**Weekly Combat Robotics Summary:** "
                "No upcoming eligible events found. "
                "Check source_status.json."
            )

        send({
            "username": "Combat Event Tracker",
            "content": message[:1900],
        })

        state["last_summary"] = local_date

    # ========================================================
    # SOURCE WARNINGS
    # ========================================================

    problems = [
        source
        for source in sources
        if (
            source["status"] in (
                "error",
                "blocked/unavailable",
            )
            or (
                source["status"] == "zero detected"
                and source["name"] in (
                    "Robot Combat Events",
                    "BuildersDB",
                )
            )
        )
    ]

    last_warning = parse_day(
        state.get(
            "last_warning",
            "",
        )
    )

    if problems and (
        not last_warning
        or TODAY - last_warning
        >= timedelta(days=7)
    ):
        warning = (
            "**Event tracker source warning:** "
            + ", ".join(
                source["name"]
                + " ("
                + source["status"]
                + ")"
                for source in problems
            )
        )

        send({
            "username": "Combat Event Tracker",
            "content": warning[:1900],
        })

        state["last_warning"] = local_date

    if (
        POST_NO_EVENT_MESSAGE
        and notifications == 0
    ):
        send({
            "username": "Combat Event Tracker",
            "content": (
                "No new nearby combat robotics "
                "events found today."
            ),
        })

    # ========================================================
    # SAVE STATE
    # ========================================================

    state["announced"] = announced

    STATE_PATH.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf8",
    )

    SNAPSHOT_PATH.write_text(
        json.dumps(
            [
                asdict(event)
                | {
                    "event_id": event.event_id
                }
                for event in events
            ],
            indent=2,
        ) + "\n",
        encoding="utf8",
    )

    STATUS_PATH.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "sources": sources,
                "skipped": skipped,
                "eligible": len(events),
                "notifications": notifications,
                "geocode_cache_size": len(
                    state.get(
                        "geocode_cache",
                        {},
                    )
                ),
            },
            indent=2,
        ) + "\n",
        encoding="utf8",
    )

    print(
        f"[COMPLETE] {len(events)} unique events; "
        f"{notifications} notifications sent"
    )


if __name__ == "__main__":
    main()
