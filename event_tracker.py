
"""ERAU ASME Combat Robotics Event Tracker.

Python 3.11+
Dependencies: requests, beautifulsoup4, python-dateutil

Required GitHub Actions secret:
    DISCORD_WEBHOOK_URL

No additional API key is required.
"""

import hashlib
import json
import math
import os
import re

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

TODAY = datetime.now(
    ZoneInfo("America/New_York")
).date()

HOME = (29.1900, -81.0480)

RADIUS = float(
    os.getenv("RADIUS_MILES", "450")
)

MAJOR_RADIUS = float(
    os.getenv("MAJOR_EVENT_RADIUS_MILES", "1100")
)

WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_URL", ""
).strip()

FORCE_SUMMARY = (
    os.getenv("FORCE_SUMMARY", "false").lower()
    == "true"
)

POST_EMPTY = (
    os.getenv("POST_NO_EVENT_MESSAGE", "false").lower()
    == "true"
)

ROLE = os.getenv("ROLE_MENTION", "").strip()

MAX_GEOCODES = int(
    os.getenv("MAX_GEOCODES", "35")
)

MAX_POSTS = int(
    os.getenv("MAX_EVENT_POSTS", "10")
)

STATE_FILE = Path("tracker_state.json")
SNAPSHOT_FILE = Path("latest_events.json")
HEALTH_FILE = Path("source_status.json")
CURATED_FILE = Path("curated_events.json")


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
        "structured",
        True,
    ),
    (
        "Robot Ruckus",
        "https://www.robotruckus.org/",
        "structured",
        True,
    ),
]


# ============================================================
# US STATES
# ============================================================

STATES = dict(
    zip(
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
        ).split("|"),
    )
)


# ============================================================
# KNOWN CITY COORDINATES
# ============================================================

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
    "columbia, sc": (34.0007, -81.0348),
    "charleston, sc": (32.7765, -79.9311),
    "greenville, sc": (34.8526, -82.3940),
    "charlotte, nc": (35.2271, -80.8431),
    "winston-salem, nc": (36.0999, -80.2442),
    "raleigh, nc": (35.7796, -78.6382),
    "hickory, nc": (35.7345, -81.3445),
    "asheville, nc": (35.5951, -82.5515),
    "birmingham, al": (33.5186, -86.8104),
    "nashville, tn": (36.1627, -86.7816),
    "chattanooga, tn": (35.0456, -85.3097),
    "norwalk, ct": (41.1176, -73.4079),
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

FULL_DATE = re.compile(
    rf"\b{MONTH}\s+\d{{1,2}}"
    rf"(?:\s*[–-]\s*(?:{MONTH}\s+)?\d{{1,2}})?"
    rf"(?:,?\s+20\d{{2}})\b",
    re.I,
)

US_LOCATION = re.compile(
    r"(?P<city>[A-Za-z][A-Za-z .'-]{1,65}),\s*"
    r"(?P<region>[A-Za-z ]{2,30})"
    r"(?:,\s*(?:United States|USA|US))?"
    r"(?=\s|$|[.;|])",
    re.I,
)

REG = re.compile(
    r"\bregistration\s+"
    r"(?:closed|open|opens?\s+(?:on\s+)?"
    r"[A-Za-z]+\s+\d{1,2},?\s+20\d{2})\b",
    re.I,
)

CLASS = re.compile(
    r"\b(?:150\s*g|1\s*lb|3\s*lb|12\s*lb|"
    r"15\s*lb|30\s*lb|60\s*lb|120\s*lb|"
    r"220\s*lb|Fairyweight|Antweight|Plastic Ant|"
    r"Beetleweight|Hobbyweight|Featherweight|"
    r"Sportsman)\b",
    re.I,
)

EVENT_HREF = re.compile(
    r"/events/\d+(?:/|\?|$)"
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]


def normalize_location(value):
    text = re.sub(
        r"\s+",
        " ",
        str(value or ""),
    ).strip().rstrip(" ,")

    text = re.sub(
        r",?\s*(?:United States|USA|US)\s*$",
        "",
        text,
        flags=re.I,
    ).strip(" ,")

    if "," not in text:
        return None

    city, region = (
        x.strip()
        for x in text.rsplit(",", 1)
    )

    if not re.fullmatch(
        r"[A-Za-z][A-Za-z .'-]{1,65}",
        city,
    ):
        return None

    if len(region) == 2:
        code = region.upper()
    else:
        code = next(
            (
                k for k, v in STATES.items()
                if v.casefold() == region.casefold()
            ),
            None,
        )

    if code not in STATES:
        return None

    return f"{city.title()}, {code}"


def parse_date(value):
    try:
        return dateparser.parse(
            str(value),
            fuzzy=True,
            default=datetime(TODAY.year, 1, 1),
        ).date().isoformat()

    except (
        ValueError,
        TypeError,
        OverflowError,
    ):
        return None


def details(text):
    found = []

    for match in CLASS.finditer(text):
        value = match.group().replace(
            " ",
            "",
        ).casefold()

        existing = [
            x.replace(" ", "").casefold()
            for x in found
        ]

        if value not in existing:
            found.append(
                match.group()
            )

    registration = REG.search(text)

    return (
        ", ".join(found[:8]) or "Not specified",
        (
            registration.group().strip()
            if registration
            else "Check event page"
        ),
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
        canonical = (
            self.source.lower(),
            self.url.rstrip("/"),
            self.title.lower(),
            self.date,
        )

        return digest(canonical)


# ============================================================
# ROBOT COMBAT EVENTS PARSING
# ============================================================

def rce_record(text):
    """Read one isolated event listing."""

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    match = FULL_DATE.search(text)

    if not match:
        return None

    title = text[
        :match.start()
    ].strip(" -–|")

    if not 3 <= len(title) <= 170:
        return None

    start = parse_date(
        match.group()
    )

    tail = text[
        match.end():
    ].lstrip()

    loc = US_LOCATION.match(tail)

    if not (
        start
        and loc
    ):
        return None

    location = normalize_location(
        loc.group()
    )

    if not location:
        return None

    classes, registration = details(
        tail[loc.end():]
    )

    return (
        title,
        start,
        location,
        classes,
        registration,
    )


def parse_rce(html, base):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    result = []
    seen = set()

    for link in soup.select("a[href]"):
        href = link.get(
            "href",
            "",
        )

        if not EVENT_HREF.search(href):
            continue

        url = urljoin(
            base,
            href,
        )

        if url in seen:
            continue

        record = rce_record(
            link.get_text(
                " ",
                strip=True,
            )
        )

        parent = link.parent

        for _ in range(5):
            if record or parent is None:
                break

            links = {
                urljoin(
                    base,
                    a.get("href", ""),
                )
                for a in parent.select("a[href]")
                if EVENT_HREF.search(
                    a.get("href", "")
                )
            }

            # Never extract from containers
            # containing multiple events.
            if links != {url}:
                break

            content = parent.get_text(
                " ",
                strip=True,
            )

            if len(content) > 550:
                break

            if len(
                FULL_DATE.findall(content)
            ) == 1:
                record = rce_record(
                    content
                )

            parent = parent.parent

        if record:
            result.append(
                Event(
                    *record[:3],
                    url,
                    "Robot Combat Events",
                    *record[3:],
                )
            )

            seen.add(url)

    return result


# ============================================================
# BUILDERSDB PARSING
# ============================================================

def parse_buildersdb(html, base):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    result = []

    for link in soup.select("a[href]"):
        text = re.sub(
            r"\s+",
            " ",
            link.get_text(
                " ",
                strip=True,
            ),
        )

        if not (
            25 <= len(text) <= 850
            and "registration:" in text.casefold()
        ):
            continue

        date_match = FULL_DATE.search(text)

        if not date_match:
            continue

        day = parse_date(
            date_match.group()
        )

        candidates = []

        for match in US_LOCATION.finditer(text):
            location = normalize_location(
                match.group()
            )

            if location:
                candidates.append(
                    (match, location)
                )

        if not (
            day
            and candidates
        ):
            continue

        locmatch, location = min(
            candidates,
            key=lambda x: abs(
                x[0].start() - date_match.end()
            ),
        )

        # Fix venue names mistakenly attached to cities.
        # Example: McWane Science Center Birmingham, AL.
        city, code = location.rsplit(
            ", ",
            1,
        )

        if location.casefold() not in CITIES:
            suffixes = [
                name
                for name in CITIES
                if (
                    name.endswith(
                        ", " + code.casefold()
                    )
                    and city.casefold().endswith(
                        name.split(", ")[0]
                    )
                )
            ]

            if suffixes:
                longest = max(
                    suffixes,
                    key=len,
                )

                location = (
                    normalize_location(longest)
                    or location
                )

        boundary = min(
            date_match.start(),
            locmatch.start(),
        )

        title = text[
            :boundary
        ].strip(" -–|")

        if not title:
            continue

        classes, registration = details(text)

        result.append(
            Event(
                title,
                day,
                location,
                urljoin(
                    base,
                    link.get("href", ""),
                ),
                "BuildersDB",
                classes,
                registration,
            )
        )

    return result


# ============================================================
# STRUCTURED EVENT PARSING
# ============================================================

def parse_structured(html, source, base, major):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    events = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)

        elif isinstance(obj, dict):
            typ = obj.get(
                "@type",
                "",
            )

            is_event = (
                typ == "Event"
                or (
                    isinstance(typ, list)
                    and "Event" in typ
                )
            )

            if is_event:
                loc = obj.get(
                    "location",
                    {},
                )

                if isinstance(loc, list):
                    loc = next(
                        (
                            x for x in loc
                            if isinstance(x, dict)
                        ),
                        {},
                    )

                if isinstance(loc, dict):
                    address = loc.get(
                        "address",
                        {},
                    )

                    if isinstance(address, dict):
                        loc = ", ".join(
                            filter(
                                None,
                                [
                                    str(
                                        address.get(
                                            "addressLocality",
                                            "",
                                        )
                                    ),
                                    str(
                                        address.get(
                                            "addressRegion",
                                            "",
                                        )
                                    ),
                                ],
                            )
                        )
                    else:
                        loc = str(
                            address
                            or loc.get("name", "")
                        )

                location = normalize_location(loc)

                start = obj.get("startDate")

                day = (
                    parse_date(start)
                    if start
                    else None
                )

                title = str(
                    obj.get("name", "")
                ).strip()

                if title and day and location:
                    classes, registration = details(
                        json.dumps(obj)
                    )

                    events.append(
                        Event(
                            title,
                            day,
                            location,
                            str(
                                obj.get("url") or base
                            ),
                            source,
                            classes,
                            registration,
                            major,
                        )
                    )

            for item in obj.values():
                if isinstance(item, (dict, list)):
                    walk(item)

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        try:
            walk(
                json.loads(
                    script.string
                    or script.get_text()
                )
            )

        except (
            ValueError,
            TypeError,
        ):
            pass

    return events


# ============================================================
# CURATED EVENTS
# ============================================================

def curated_events():
    if not CURATED_FILE.exists():
        return []

    data = json.loads(
        CURATED_FILE.read_text(
            encoding="utf-8"
        )
    )

    if not isinstance(data, list):
        raise ValueError(
            "curated_events.json must contain a JSON list"
        )

    return [
        Event(
            str(e["title"]),
            str(e["date"]),
            str(e["location"]),
            str(e.get("url", "")),
            "Curated",
            str(
                e.get(
                    "classes",
                    "Not specified",
                )
            ),
            str(
                e.get(
                    "registration",
                    "Check event page",
                )
            ),
            bool(
                e.get("major", False)
            ),
        )
        for e in data
    ]


# ============================================================
# HTTP SESSION
# ============================================================

def session_new():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "ERAU-ASME-Combat-Robotics-Event-Tracker/4.0 "
            "(educational event monitor)"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })

    retry = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=(
            429,
            500,
            502,
            503,
            504,
        ),
        respect_retry_after_header=True,
    )

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry),
    )

    return session


def fetch(session, url):
    response = session.get(
        url,
        timeout=25,
    )

    response.raise_for_status()

    return response.text


# ============================================================
# DISTANCE CALCULATION
# ============================================================

def miles_from(lat, lon):
    a = math.radians(
        HOME[0]
    )

    b = math.radians(lat)

    dp = b - a
    dl = math.radians(
        lon - HOME[1]
    )

    x = (
        math.sin(dp / 2) ** 2
        + math.cos(a)
        * math.cos(b)
        * math.sin(dl / 2) ** 2
    )

    return (
        3958.7613
        * 2
        * math.atan2(
            math.sqrt(x),
            math.sqrt(1 - x),
        )
    )


# ============================================================
# AUTOMATIC GEOCODING
# ============================================================

def coordinates(loc, state, session):
    key = loc.casefold()

    if key in CITIES:
        return CITIES[key]

    cache = state.setdefault(
        "geocode_cache",
        {},
    )

    entry = cache.get(key)

    if (
        isinstance(entry, list)
        and len(entry) == 2
    ):
        return tuple(entry)

    # Failed lookups expire after 30 days.
    if (
        isinstance(entry, dict)
        and entry.get("missing")
    ):
        checked = parse_date(
            entry.get("checked")
        )

        if (
            checked
            and TODAY - date.fromisoformat(checked)
            < timedelta(days=30)
        ):
            return None

    attempted = state.setdefault(
        "_geo_attempted",
        set(),
    )

    if key in attempted:
        return None

    if len(attempted) >= MAX_GEOCODES:
        print(
            f"[GEO] Daily limit reached: {loc}"
        )
        return None

    attempted.add(key)

    city, code = loc.rsplit(
        ", ",
        1,
    )

    try:
        response = session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": f"{city}, {STATES[code]}",
                "count": 30,
                "countryCode": "US",
                "language": "en",
            },
            timeout=15,
        )

        response.raise_for_status()

        choices = (
            response.json().get("results")
            or []
        )

        matches = [
            x for x in choices
            if (
                x.get("name", "").casefold()
                == city.casefold()
                and x.get("admin1", "").casefold()
                == STATES[code].casefold()
                and x.get("country_code") == "US"
            )
        ]

        if matches:
            coords = [
                float(matches[0]["latitude"]),
                float(matches[0]["longitude"]),
            ]

            cache[key] = coords

            print(
                f"[GEO] {loc}: resolved"
            )

            return tuple(coords)

        cache[key] = {
            "missing": True,
            "checked": TODAY.isoformat(),
        }

        print(
            f"[GEO] {loc}: no verified match"
        )

    except (
        requests.RequestException,
        TypeError,
        KeyError,
        ValueError,
    ) as exc:
        print(
            f"[GEO] {loc}: temporary lookup error "
            f"({type(exc).__name__})"
        )

    return None


# ============================================================
# CROSS-SOURCE DUPLICATE DETECTION
# ============================================================

def normalized_title(event):
    title = event.title.casefold().strip()

    city, code = event.location.rsplit(
        ", ",
        1,
    )

    suffixes = (
        f"{city}, {code}",
        f"{city}, {STATES[code]}",
        f"{city}, {code}, united states",
        f"{city}, {STATES[code]}, united states",
    )

    for tail in suffixes:
        if title.endswith(tail.casefold()):
            title = title[
                :-len(tail)
            ].strip(" -–|,")

    title = re.sub(
        r"\bregistration\s+"
        r"(?:open|closed|opens).*$",
        "",
        title,
        flags=re.I,
    ).strip()

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        title,
    ).strip()


def is_duplicate(a, b):
    if (
        a.date != b.date
        or a.location.casefold()
        != b.location.casefold()
    ):
        return False

    x = normalized_title(a)
    y = normalized_title(b)

    if not x or not y:
        return False

    return (
        x == y
        or (
            len(x) >= 12
            and len(y) >= 12
            and SequenceMatcher(
                None,
                x,
                y,
            ).ratio() >= 0.92
        )
    )


def merge_events(events):
    unique = []
    duplicates = 0

    priority = {
        "Robot Combat Events": 5,
        "BuildersDB": 4,
        "NHRL": 3,
        "Robot Ruckus": 3,
        "Curated": 1,
    }

    for event in events:
        existing = next(
            (
                x for x in unique
                if is_duplicate(
                    x,
                    event,
                )
            ),
            None,
        )

        if not existing:
            event._aliases = {
                event.event_id
            }

            unique.append(event)
            continue

        duplicates += 1

        aliases = (
            existing._aliases
            | {event.event_id}
        )

        if (
            priority.get(event.source, 0)
            > priority.get(existing.source, 0)
        ):
            event._aliases = aliases

            unique[
                unique.index(existing)
            ] = event
        else:
            existing._aliases = aliases

    print(
        f"[DEDUP] Removed {duplicates} "
        "duplicate event records"
    )

    return sorted(
        unique,
        key=lambda e: (
            e.date,
            e.distance_miles,
            e.title,
        ),
    )


# ============================================================
# COLLECT ALL EVENTS
# ============================================================

def collect(state):
    session = session_new()

    raw = []
    sources = []

    for name, url, kind, major in SOURCES:
        urls = (
            [
                url,
                "https://www.robotcombatevents.com/event_map",
            ]
            if kind == "rce"
            else [url]
        )

        parsed = []
        attempts = []

        for candidate in urls:
            try:
                html = fetch(
                    session,
                    candidate,
                )

                parsers = {
                    "rce": lambda: parse_rce(
                        html,
                        candidate,
                    ),
                    "buildersdb": lambda: parse_buildersdb(
                        html,
                        candidate,
                    ),
                    "structured": lambda: parse_structured(
                        html,
                        name,
                        candidate,
                        major,
                    ),
                }

                parsed = parsers[kind]()

                attempts.append(
                    f"HTTP 200; {len(parsed)} parsed "
                    f"from {candidate}"
                )

                if parsed:
                    break

            except requests.RequestException as exc:
                status_code = getattr(
                    getattr(
                        exc,
                        "response",
                        None,
                    ),
                    "status_code",
                    "?",
                )

                attempts.append(
                    f"{candidate}: request failed "
                    f"({type(exc).__name__}, "
                    f"HTTP {status_code})"
                )

            except Exception as exc:
                attempts.append(
                    f"{candidate}: parser failure "
                    f"{type(exc).__name__}: "
                    f"{str(exc)[:120]}"
                )

        status = (
            "ok"
            if parsed
            else (
                "error"
                if (
                    attempts
                    and all(
                        "failed" in a or "failure" in a
                        for a in attempts
                    )
                )
                else "zero detected"
            )
        )

        sources.append({
            "name": name,
            "url": url,
            "parsed": len(parsed),
            "status": status,
            "detail": "; ".join(attempts),
        })

        print(
            f"[SOURCE] {name}: "
            f"{len(parsed)} events ({status})"
        )

        if status != "ok":
            print(
                f"[SOURCE DETAIL] "
                f"{sources[-1]['detail']}"
            )

        raw.extend(parsed)

    curated = curated_events()
    raw.extend(curated)

    print(
        f"[SOURCE] Curated: "
        f"{len(curated)} entries"
    )

    skipped = {
        "old": 0,
        "bad_date": 0,
        "bad_location": 0,
        "unresolved_location": 0,
        "out_of_radius": 0,
    }

    eligible = []

    # Prioritize southeastern US locations before
    # using the geocoding request budget.
    nearby_states = {
        "FL",
        "GA",
        "SC",
        "NC",
        "AL",
        "TN",
    }

    raw.sort(
        key=lambda event: (
            0
            if (
                normalize_location(event.location)
                or ""
            ).split(", ")[-1] in nearby_states
            else 1,
            event.date,
        )
    )

    for event in raw:
        day = parse_date(
            event.date
        )

        if not day:
            skipped["bad_date"] += 1
            continue

        if day < TODAY.isoformat():
            skipped["old"] += 1
            continue

        location = normalize_location(
            event.location
        )

        if not location:
            skipped["bad_location"] += 1
            continue

        event.date = day
        event.location = location

        coords = coordinates(
            location,
            state,
            session,
        )

        if not coords:
            skipped["unresolved_location"] += 1
            continue

        event.distance_miles = round(
            miles_from(*coords),
            1,
        )

        radius = (
            MAJOR_RADIUS
            if event.major
            else RADIUS
        )

        if event.distance_miles > radius:
            skipped["out_of_radius"] += 1
            continue

        eligible.append(event)

    result = merge_events(
        eligible
    )

    print(
        f"[FILTER] {len(raw)} extracted, "
        f"{len(result)} unique eligible; "
        f"skipped={skipped}"
    )

    return (
        result,
        sources,
        skipped,
    )


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    if STATE_FILE.exists():
        state = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
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

    old_path = Path(
        "posted_events.json"
    )

    old = (
        json.loads(
            old_path.read_text(
                encoding="utf-8"
            )
        )
        if old_path.exists()
        else []
    )

    return {
        "announced": {
            str(x): {
                "registration": "",
            }
            for x in old
        },
        "geocode_cache": {},
        "last_summary": "",
        "last_warning": "",
    }


# ============================================================
# DISCORD DELIVERY
# ============================================================

def send(content, embed=None):
    if not WEBHOOK:
        raise RuntimeError(
            "Missing DISCORD_WEBHOOK_URL "
            "GitHub Actions secret"
        )

    payload = {
        "username": "Combat Event Tracker",
        "content": (
            (
                ROLE + "\n"
                if ROLE and embed
                else ""
            )
            + content
        ),
        "allowed_mentions": {
            "parse": [],
            "roles": (
                [ROLE.strip("<@&>")]
                if ROLE.startswith("<@&")
                else []
            ),
        },
    }

    if embed:
        payload["embeds"] = [embed]

    response = requests.post(
        WEBHOOK,
        json=payload,
        timeout=25,
    )

    response.raise_for_status()


def event_embed(event, update=False):
    fields = [
        ("Date", event.date),
        ("Location", event.location),
        (
            "Distance",
            f"{event.distance_miles:.0f} miles "
            "straight-line from ERAU",
        ),
        ("Weight classes", event.classes),
        ("Registration", event.registration),
        ("Source", event.source),
    ]

    title = (
        (
            "Event updated: "
            if update
            else "New event: "
        )
        + event.title
    )

    return {
        "title": title[:256],
        "url": (
            event.url
            if event.url.startswith(
                ("http://", "https://")
            )
            else None
        ),
        "color": (
            0xF2AA4C
            if update
            else 0x2878CF
        ),
        "fields": [
            {
                "name": name,
                "value": str(value)[:1024] or "Unknown",
                "inline": (
                    name != "Weight classes"
                ),
            }
            for name, value in fields
        ],
        "footer": {
            "text": (
                "Verify dates and registration "
                "with the organizer"
            )
        },
    }


# ============================================================
# SAVE STATE AND DIAGNOSTICS
# ============================================================

def save(state, events, sources, skipped, posts):
    state.pop(
        "_geo_attempted",
        None,
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    SNAPSHOT_FILE.write_text(
        json.dumps(
            [
                asdict(event)
                | {"event_id": event.event_id}
                for event in events
            ],
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    HEALTH_FILE.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "sources": sources,
                "skipped": skipped,
                "eligible": len(events),
                "notifications": posts,
                "geocoded_cities": len(
                    state.get(
                        "geocode_cache",
                        {},
                    )
                ),
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    state = load_state()

    events, sources, skipped = collect(
        state
    )

    announced = state["announced"]

    known = {}

    for event in events:
        known[event.event_id] = next(
            (
                announced[alias]
                for alias in sorted(
                    event._aliases
                )
                if alias in announced
            ),
            None,
        )

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
                "registration",
                "",
            ).lower()
            not in (
                "",
                "check event page",
            )
            and event.registration.lower()
            not in (
                "check event page",
                "not specified",
            )
            and known[event.event_id][
                "registration"
            ].casefold()
            != event.registration.casefold()
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

    posts = 0

    for event in (
        new_events + updated_events
    )[:MAX_POSTS]:
        is_update = event in updated_events

        content = (
            "Registration information changed"
            if is_update
            else "New nearby combat robotics event"
        )

        send(
            content,
            event_embed(
                event,
                is_update,
            ),
        )

        # Mark every matching source ID after delivery.
        for alias in event._aliases:
            announced[alias] = {
                "registration": event.registration,
                "date": event.date,
            }

        posts += 1

    # Preserve historical IDs for merged listings.
    for event in events:
        if (
            event not in new_events
            and event not in updated_events
            and known[event.event_id] is not None
        ):
            for alias in event._aliases:
                announced[alias] = known[
                    event.event_id
                ]

    stamp = TODAY.isoformat()

    # ========================================================
    # WEEKLY SUMMARY
    # ========================================================

    if (
        TODAY.weekday() == 0
        or FORCE_SUMMARY
    ) and state.get(
        "last_summary"
    ) != stamp:

        lines = [
            (
                f"**{event.date}** • {event.title} • "
                f"{event.location} "
                f"({event.distance_miles:.0f} mi straight-line)"
            )
            for event in events[:10]
        ]

        if lines:
            body = (
                "**Upcoming combat robotics events near ERAU**\n"
                + "\n".join(lines)
            )
        else:
            body = (
                "**Weekly summary:** "
                "No upcoming eligible events detected. "
                "Review source_status.json."
            )

        send(
            body[:1900]
        )

        state["last_summary"] = stamp

    # ========================================================
    # SOURCE WARNINGS
    # ========================================================

    problems = [
        source
        for source in sources
        if (
            source["status"] in (
                "error",
                "zero detected",
            )
            and source["name"] in (
                "Robot Combat Events",
                "BuildersDB",
            )
        )
    ]

    last = parse_date(
        state.get(
            "last_warning",
            "",
        )
    )

    if problems and (
        not last
        or (
            TODAY - date.fromisoformat(last)
            >= timedelta(days=7)
        )
    ):
        warning = (
            "**Source warning:** "
            + ", ".join(
                f"{source['name']} "
                f"({source['status']})"
                for source in problems
            )
        )

        send(warning)

        state["last_warning"] = stamp

    if POST_EMPTY and not posts:
        send(
            "No new nearby combat robotics "
            "events found today."
        )

    save(
        state,
        events,
        sources,
        skipped,
        posts,
    )

    print(
        f"[COMPLETE] {len(events)} unique events; "
        f"{posts} notifications sent"
    )


if __name__ == "__main__":
    main()
