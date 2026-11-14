
"""ERAU Combat Robotics Event Tracker.

Daily event discovery, Discord notifications, weekly summaries,
duplicate prevention, and source diagnostics.

Dependencies: requests, beautifulsoup4, python-dateutil
Store DISCORD_WEBHOOK_URL in GitHub Actions secrets.
"""

import hashlib
import json
import math
import os
import re

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser


# ============================================================
# CONFIGURATION
# ============================================================

HOME_LAT = 29.1900
HOME_LON = -81.0480

RADIUS_MILES = float(os.getenv("RADIUS_MILES", "450"))
MAJOR_RADIUS_MILES = float(
    os.getenv("MAJOR_EVENT_RADIUS_MILES", "1100")
)

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
ROLE_MENTION = os.getenv("ROLE_MENTION", "").strip()

POST_NO_EVENT_MESSAGE = (
    os.getenv("POST_NO_EVENT_MESSAGE", "false").lower() == "true"
)

FORCE_SUMMARY = (
    os.getenv("FORCE_SUMMARY", "false").lower() == "true"
)

STATE_PATH = Path("tracker_state.json")
OLD_STATE_PATH = Path("posted_events.json")
SNAPSHOT_PATH = Path("latest_events.json")
STATUS_PATH = Path("source_status.json")

TODAY = datetime.now(
    ZoneInfo("America/New_York")
).date()


# ============================================================
# EVENT SOURCES
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


# ============================================================
# LOCATION DATABASE
# ============================================================

# Coordinates are used for straight-line distance calculations.
# Unknown cities are skipped rather than assigned an
# incorrect location or distance.

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

STATE_NAMES = {
    "Florida": "FL",
    "Georgia": "GA",
    "Alabama": "AL",
    "South Carolina": "SC",
    "North Carolina": "NC",
    "Tennessee": "TN",
    "Connecticut": "CT",
}

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)

DATE_REGEX = re.compile(
    rf"\b(?:{MONTH})\s+\d{{1,2}}"
    rf"(?:,?\s+20\d{{2}})?|"
    rf"\b20\d{{2}}-\d{{2}}-\d{{2}}",
    re.I,
)

LOCATION_REGEX = re.compile(
    r"([A-Z][A-Za-z .'-]{1,60}),\s*"
    r"(FL|GA|SC|NC|AL|TN|CT|NJ|PA|VA|KY|MS|LA)\b",
    re.I,
)

REG_REGEX = re.compile(
    r"(Registration\s+(?:closed|open|"
    r"opens\s+(?:\w+\s+){1,3}\d{1,2},?\s+20\d{2}))",
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


# ============================================================
# EVENT DATA MODEL
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

        return hashlib.sha256(
            json.dumps(canonical).encode()
        ).hexdigest()[:16]


# ============================================================
# DISTANCE CALCULATION
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
        3958.7613
        * 2
        * math.atan2(math.sqrt(x), math.sqrt(1 - x))
    )


# ============================================================
# DATE AND LOCATION HELPERS
# ============================================================

def parse_day(value):
    try:
        return dtparser.parse(
            value,
            fuzzy=True,
            default=datetime(TODAY.year, 1, 1),
        ).date()
    except (ValueError, TypeError, OverflowError):
        return None


def find_date(text):
    match = DATE_REGEX.search(text)

    if not match:
        return None

    snippet = match.group(0)

    if not re.search(r"20\d{2}", snippet):
        nearby = text[match.start():match.start() + 110]
        year = re.search(r"20\d{2}", nearby)

        if not year:
            return None

        snippet += ", " + year.group(0)

    dt = parse_day(snippet)

    return dt.isoformat() if dt else None


def location_from(text):
    for full, abbreviation in STATE_NAMES.items():
        text = re.sub(
            r",\s*" + re.escape(full) + r"\b",
            ", " + abbreviation,
            text,
            flags=re.I,
        )

    lower = text.lower()

    matches = [
        (lower.rfind(city), city)
        for city in CITIES
        if city in lower
    ]

    if matches:
        city = max(matches)[1]
        name, state = city.rsplit(", ", 1)
        return f"{name.title()}, {state.upper()}"

    match = LOCATION_REGEX.search(text)

    if match:
        return match.group(0)

    return ""


def details_from(text):
    classes = list(
        dict.fromkeys(
            match.group(0)
            for match in WEIGHTS_REGEX.finditer(text)
        )
    )

    registration_match = REG_REGEX.search(text)

    registration = (
        registration_match.group(0).strip()
        if registration_match
        else "Check event page"
    )

    return (
        ", ".join(classes[:8]) if classes else "Not specified",
        registration,
    )


# ============================================================
# BUILDERSDB PARSER
# ============================================================

def parse_buildersdb(html):
    soup = BeautifulSoup(html, "html.parser")
    result = []

    for link in soup.select("a[href]"):
        text = link.get_text(" ", strip=True)

        if (
            len(text) < 25
            or len(text) > 800
            or "Registration:" not in text
        ):
            continue

        day = find_date(text)
        location = location_from(text)

        if not day or not location:
            continue

        location_start = text.lower().find(
            location.lower()
        )

        title = (
            text[:location_start].strip(" -")
            if location_start > 0
            else text[:75]
        )

        if not title:
            continue

        classes, registration = details_from(text)

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
# STRUCTURED EVENT DATA PARSER
# ============================================================

def parse_jsonld_events(soup, source, major=False):
    result = []

    def explore(value):
        if isinstance(value, list):
            for item in value:
                explore(item)

        elif isinstance(value, dict):
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
                start = value.get("startDate", "")

                dt = parse_day(start)
                location = value.get("location") or {}

                if isinstance(location, dict):
                    address = location.get("address") or {}

                    if isinstance(address, dict):
                        location = ", ".join(
                            str(x)
                            for x in [
                                address.get("addressLocality", ""),
                                address.get("addressRegion", ""),
                            ]
                            if x
                        )
                    else:
                        location = str(
                            address or location.get("name", "")
                        )

                if title and dt and location:
                    classes, registration = details_from(
                        json.dumps(value)
                    )

                    result.append(
                        Event(
                            title=str(title),
                            date=dt.isoformat(),
                            location=str(location),
                            url=str(value.get("url", "")),
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
                script.string or script.get_text()
            )
            explore(data)

        except (TypeError, ValueError):
            continue

    return result


# ============================================================
# ROBOT COMBAT EVENTS PARSER - CORRECTED
# ============================================================

def parse_rce_card(text):
    """
    Parse one Robot Combat Events listing independently.

    A date and location must appear together inside the
    same event listing. No borrowing data from other cards.
    """

    text = re.sub(r"\s+", " ", text).strip()

    date_match = re.search(
        rf"\b({MONTH})\s+(\d{{1,2}})"
        rf"(?:\s*[–-]\s*(?:{MONTH}\s+)?\d{{1,2}})?"
        rf",?\s+(20\d{{2}})\b",
        text,
        re.I,
    )

    if not date_match:
        return None

    title = text[:date_match.start()].strip(" -–|")

    if not (3 <= len(title) <= 170):
        return None

    day = parse_day(
        f"{date_match.group(1)} "
        f"{date_match.group(2)}, "
        f"{date_match.group(3)}"
    )

    if not day:
        return None

    tail = text[date_match.end():].strip()

    # Location must follow the event date.
    # This prevents unrelated dates and cities being mixed.

    location_match = re.match(
        r"(?P<city>[A-Za-z][A-Za-z .'-]{1,65}),"
        r"\s*(?P<state>[A-Z]{2})"
        r"(?:,\s*United States)?(?=\s|$)",
        tail,
    )

    if not location_match:
        return None

    location = (
        f"{location_match.group('city').strip()}, "
        f"{location_match.group('state')}"
    )

    # Reject cities we cannot accurately locate.
    if location.lower() not in CITIES:
        return None

    own_details = tail[location_match.end():]

    classes, registration = details_from(own_details)

    return (
        title,
        day.isoformat(),
        location,
        classes,
        registration,
    )


def parse_rce(html, base):
    """
    Extract individually validated events.

    Never use a shared HTML container containing multiple
    competitions to determine an event's date or location.
    """

    soup = BeautifulSoup(html, "html.parser")

    found = []
    used = set()

    for link in soup.select("a[href]"):
        href = link.get("href", "")

        if not re.search(
            r"/events/\d+(?:/|\?|$)",
            href,
        ):
            continue

        url = urljoin(base, href)

        if url.rstrip("/") in used:
            continue

        # First try parsing the link's own text.
        card_text = link.get_text(" ", strip=True)
        record = parse_rce_card(card_text)

        if record is None:
            ancestor = link.parent

            # Only inspect the smallest parent containers.
            # A parent must contain exactly one event link.
            for _ in range(4):
                if ancestor is None:
                    break

                event_links = [
                    a
                    for a in ancestor.select("a[href]")
                    if re.search(
                        r"/events/\d+(?:/|\?|$)",
                        a.get("href", ""),
                    )
                ]

                if len(event_links) != 1:
                    break

                candidate_text = ancestor.get_text(
                    " ",
                    strip=True,
                )

                if len(candidate_text) > 650:
                    break

                # A shared container with multiple dates
                # is unsafe to use as an individual card.
                if len(
                    DATE_REGEX.findall(candidate_text)
                ) == 1:
                    record = parse_rce_card(
                        candidate_text
                    )

                    if record is not None:
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

        used.add(url.rstrip("/"))

    return found


# ============================================================
# ROBOT COMBAT EVENTS SOURCE HANDLING
# ============================================================

def get_rce_events(session):
    urls = [
        "https://www.robotcombatevents.com/events/",
        "https://www.robotcombatevents.com/event_map",
    ]

    diagnostics = []

    for url in urls:
        try:
            html = fetch(session, url)
            events = parse_rce(html, url)

            diagnostics.append(
                f"{url}: HTTP 200; extracted "
                f"{len(events)} events"
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
                f"({type(exc).__name__}: "
                f"{str(exc)[:90]})"
            )

    blocked = (
        diagnostics
        and all(
            "HTTP 403" in x or "HTTP 429" in x
            for x in diagnostics
        )
    )

    status = (
        "blocked/unavailable"
        if blocked
        else "zero detected"
    )

    return [], status, "; ".join(diagnostics)


# ============================================================
# NHRL PARSER
# ============================================================

def parse_nhrl(html, base):
    soup = BeautifulSoup(html, "html.parser")

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

        if parent.parent:
            text = parent.parent.get_text(
                " ",
                strip=True,
            )[:700]
        else:
            text = parent.get_text(
                " ",
                strip=True,
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
# ROBOT RUCKUS PARSER
# ============================================================

def parse_ruckus(html, base):
    soup = BeautifulSoup(html, "html.parser")

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
        date_text = (
            f"{match.group(1)} "
            f"{match.group(2)}, "
            f"{match.group(3)}"
        )

        day = parse_day(date_text)

        if day:
            result.append(
                Event(
                    title="Robot Ruckus " + match.group(3),
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
    path = Path("curated_events.json")

    if not path.exists():
        return []

    try:
        data = json.loads(
            path.read_text(encoding="utf8")
        )

        return [
            Event(
                title=str(e["title"]),
                date=str(e["date"]),
                location=str(e["location"]),
                url=str(e["url"]),
                source="Curated",
                classes=str(
                    e.get(
                        "classes",
                        "Not specified",
                    )
                ),
                registration=str(
                    e.get(
                        "registration",
                        "Check event page",
                    )
                ),
                major=bool(
                    e.get("major", False)
                ),
            )
            for e in data
        ]

    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"Invalid curated_events.json: {exc}"
        ) from exc


# ============================================================
# EVENT HISTORY
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
            str(x): {
                "registration": "",
            }
            for x in old_ids
        },
        "last_summary": "",
        "last_warning": "",
    }


# ============================================================
# DISCORD WEBHOOK
# ============================================================

def send(payload):
    if not WEBHOOK:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL GitHub secret is missing"
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
            f"straight-line from ERAU"
        )
    else:
        distance_text = "Unknown"

    title_prefix = (
        "Event updated: "
        if update
        else "New event: "
    )

    return {
        "title": (
            title_prefix + event.title
        )[:256],
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
                "value": event.location[:100],
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
# COLLECT AND FILTER EVENTS
# ============================================================

def collect():
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
            found, status, diagnostic = get_rce_events(
                session
            )

            sources.append({
                "name": name,
                "url": url,
                "parsed": len(found),
                "status": status,
                "detail": diagnostic,
            })

            print(
                f"[SOURCE] {name}: "
                f"{len(found)} events extracted "
                f"({status})"
            )

            print(
                f"[SOURCE DETAIL] {diagnostic}"
            )

            raw.extend(found)
            continue

        try:
            html = fetch(
                session,
                url,
            )

            parsers = {
                "buildersdb": lambda: parse_buildersdb(html),
                "nhrl": lambda: parse_nhrl(html, url),
                "ruckus": lambda: parse_ruckus(html, url),
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

    unique = {}

    for event in raw:
        dt = parse_day(event.date)

        if not dt:
            skipped["unknown_date"] += 1
            continue

        if dt < TODAY:
            skipped["old"] += 1
            continue

        event.date = dt.isoformat()

        location = event.location.lower().strip()

        coordinates = CITIES.get(location)

        if coordinates is None:
            skipped["unknown_location"] += 1
            continue

        event.distance_miles = round(
            distance(*coordinates),
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

        # Deduplicate equivalent records from different
        # sources while preferring automatically detected
        # records over curated fallback records.

        fingerprint = (
            re.sub(
                r"[^a-z0-9]+",
                "",
                event.title.lower(),
            ),
            event.date,
            event.location.lower(),
        )

        if (
            fingerprint not in unique
            or (
                unique[fingerprint].source == "Curated"
                and event.source != "Curated"
            )
        ):
            unique[fingerprint] = event

    result = sorted(
        unique.values(),
        key=lambda e: (
            e.date,
            e.distance_miles,
            e.title,
        ),
    )

    print(
        f"[FILTER] {len(raw)} extracted total; "
        f"{len(result)} eligible; "
        f"skipped={skipped}"
    )

    return result, sources, skipped


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():
    events, sources, skipped = collect()

    state = load_state()
    announced = state["announced"]

    new_events = [
        event
        for event in events
        if event.event_id not in announced
    ]

    updated_events = [
        event
        for event in events
        if (
            event.event_id in announced
            and announced[event.event_id].get(
                "registration", ""
            )
            and announced[event.event_id]["registration"]
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

    # Limit event notifications per run.
    # Only mark an event announced after delivery succeeds.

    notifications = 0

    for event in (
        new_events + updated_events
    )[:10]:

        is_update = event in updated_events

        message = (
            "Registration information changed"
            if is_update
            else "New nearby combat robotics event"
        )

        content = (
            (ROLE_MENTION + "\n" if ROLE_MENTION else "")
            + message
        )

        payload = {
            "username": "Combat Event Tracker",
            "content": content,
            "embeds": [
                embed(event, is_update)
            ],
        }

        send(payload)

        announced[event.event_id] = {
            "registration": event.registration,
            "date": event.date,
        }

        notifications += 1

    # Weekly summary every Monday.

    local_date = TODAY.isoformat()

    should_summarize = (
        TODAY.weekday() == 0
        or FORCE_SUMMARY
    )

    if (
        should_summarize
        and state.get("last_summary") != local_date
    ):
        summary = sorted(
            events,
            key=lambda e: (
                e.date,
                e.distance_miles,
            ),
        )[:10]

        if summary:
            lines = [
                (
                    f"**{e.date}** • {e.title} • "
                    f"{e.location} "
                    f"({e.distance_miles:.0f} mi straight-line)"
                )
                for e in summary
            ]

            message = (
                "**Upcoming combat robotics events near ERAU**\n"
                + "\n".join(lines)
            )

        else:
            message = (
                "**Weekly combat robotics summary:** "
                "No upcoming eligible events found. "
                "Check source health in GitHub Actions."
            )

        send({
            "username": "Combat Event Tracker",
            "content": message[:1900],
        })

        state["last_summary"] = local_date

    # Send a source warning no more than once per week.

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
                and source["name"].startswith(
                    (
                        "Robot Combat Events",
                        "BuildersDB",
                    )
                )
            )
        )
    ]

    last_warning = parse_day(
        state.get("last_warning", "")
    )

    if problems and (
        not last_warning
        or TODAY - last_warning >= timedelta(days=7)
    ):
        message = (
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
            "content": message[:1900],
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

    # Save event history and source diagnostics.

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
                | {"event_id": event.event_id}
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
            },
            indent=2,
        ) + "\n",
        encoding="utf8",
    )


if __name__ == "__main__":
    main()
