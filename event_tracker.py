import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, date
from typing import Iterable, List, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


HOME_NAME = "ERAU Daytona Beach"
HOME_LAT = 29.1900
HOME_LON = -81.0480
RADIUS_MILES = float(os.getenv("RADIUS_MILES", "450"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
POSTED_FILE = "posted_events.json"

SOURCES = [
    {
        "name": "RobotCombatEvents",
        "url": "https://www.robotcombatevents.com/event_map",
        "type": "rce",
    },
    {
        "name": "BuildersDB",
        "url": "https://www.buildersdb.com/events?show=all",
        "type": "buildersdb",
    },
]

# Enough to make the first version useful near ERAU.
# Add cities as you see new events.
CITY_COORDS = {
    "daytona beach, fl": (29.2108, -81.0228),
    "orlando, fl": (28.5383, -81.3792),
    "tampa, fl": (27.9506, -82.4572),
    "jacksonville, fl": (30.3322, -81.6557),
    "jensen beach, fl": (27.2545, -80.2298),
    "miami, fl": (25.7617, -80.1918),
    "west palm beach, fl": (26.7153, -80.0534),
    "fort lauderdale, fl": (26.1224, -80.1373),
    "gainesville, fl": (29.6516, -82.3248),
    "tallahassee, fl": (30.4383, -84.2807),
    "pensacola, fl": (30.4213, -87.2169),
    "charlotte, nc": (35.2271, -80.8431),
    "winston-salem, nc": (36.0999, -80.2442),
    "raleigh, nc": (35.7796, -78.6382),
    "atlanta, ga": (33.7490, -84.3880),
    "savannah, ga": (32.0809, -81.0912),
    "columbia, sc": (34.0007, -81.0348),
    "charleston, sc": (32.7765, -79.9311),
    "birmingham, al": (33.5186, -86.8104),
    "harrisburg, pa": (40.2732, -76.8867),
    "philadelphia, pa": (39.9526, -75.1652),
    "washington, nj": (40.7584, -74.9793),
    "north brunswick township, nj": (40.4520, -74.4829),
    "phillipsburg, nj": (40.6937, -75.1902),
    "frankford, nj": (41.1265, -74.7716),
}

STATE_APPROX = {
    "FL": (28.0, -82.0),
    "GA": (32.7, -83.3),
    "SC": (33.8, -80.9),
    "NC": (35.5, -79.4),
    "AL": (32.8, -86.8),
    "TN": (35.8, -86.4),
    "MS": (32.7, -89.7),
}


@dataclass
class Event:
    title: str
    date_text: str
    location: str
    url: str
    source: str
    distance_miles: Optional[float] = None

    @property
    def event_id(self) -> str:
        raw = f"{self.source}|{self.title}|{self.date_text}|{self.location}|{self.url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    radius = 3958.7613
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def normalize_location(location: str) -> str:
    location = re.sub(r"\s+", " ", location.strip())
    location = location.replace(", United States", "")
    location = location.replace(", US", "")
    return location


def find_coords(location: str):
    loc = normalize_location(location)
    parts = [p.strip() for p in loc.split(",")]

    if len(parts) >= 2:
        city = parts[0].lower()
        state = parts[1].strip().upper()
        key = f"{city}, {state.lower()}"
        if key in CITY_COORDS:
            return CITY_COORDS[key]
        if state in STATE_APPROX:
            return STATE_APPROX[state]

    return None


def event_is_futureish(date_text: str) -> bool:
    # Keeps events dated today or later. If parsing fails, keep it so humans can review.
    try:
        first_date = re.split(r"\s+-\s+", date_text.strip())[0]
        dt = date_parser.parse(first_date, fuzzy=True).date()
        return dt >= date.today()
    except Exception:
        return True


def distance_for(location: str) -> Optional[float]:
    coords = find_coords(location)
    if not coords:
        return None
    return haversine_miles(HOME_LAT, HOME_LON, coords[0], coords[1])


def keep_event(event: Event) -> bool:
    if not event_is_futureish(event.date_text):
        return False

    event.distance_miles = distance_for(event.location)

    if event.distance_miles is None:
        # Keep unknown-distance Southeast/Florida-looking events for review.
        loc_upper = event.location.upper()
        return any(token in loc_upper for token in [" FL", ",FL", " FLORIDA", " GA", " NC", " SC", " AL", " TN"])

    return event.distance_miles <= RADIUS_MILES


def fetch(url: str) -> str:
    headers = {"User-Agent": "ERAU-ASME-Combat-Robotics-Event-Tracker/1.0"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def parse_rce(html: str, source_url: str) -> List[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for h3 in soup.find_all("h3"):
        link = h3.find("a")
        if not link:
            continue

        title = link.get_text(" ", strip=True)
        date_text = h3.get_text(" ", strip=True).replace(title, "").strip()
        date_text = date_text.replace("\xa0", " ").strip()

        loc_tag = h3.find_next("h4")
        location = loc_tag.get_text(" ", strip=True) if loc_tag else ""

        href = link.get("href", "")
        if href.startswith("/"):
            url = "https://www.robotcombatevents.com" + href
        else:
            url = href or source_url

        if title and location:
            events.append(Event(title, date_text, normalize_location(location), url, "RobotCombatEvents"))

    return events


def parse_buildersdb(html: str, source_url: str) -> List[Event]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    # BuildersDB text is compact in fetched HTML. This catches common listing format:
    # Title City, ST, US Month DD, YYYY - Month DD, YYYY
    pattern = re.compile(
        r"(?P<title>[A-Z0-9][^\n]{3,100}?)\s+"
        r"(?P<city>[A-Za-z .'\-]+),\s*(?P<state>[A-Z]{2}|[A-Za-z ]+),\s*(?P<country>US|United States|CA|Canada)\s+"
        r"(?P<date>[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}(?:\s+-\s+[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})?)"
    )

    events = []
    seen = set()

    for match in pattern.finditer(text):
        title = match.group("title").strip()
        location = f"{match.group('city').strip()}, {match.group('state').strip()}, {match.group('country').strip()}"
        date_text = match.group("date").strip()
        key = (title, location, date_text)

        if key in seen:
            continue
        seen.add(key)

        events.append(Event(title, date_text, normalize_location(location), source_url, "BuildersDB"))

    return events


def load_posted_ids() -> set:
    if not os.path.exists(POSTED_FILE):
        return set()
    with open(POSTED_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_posted_ids(ids: Iterable[str]) -> None:
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def discord_embed(event: Event) -> dict:
    distance = "Unknown"
    if event.distance_miles is not None:
        distance = f"{event.distance_miles:.0f} miles from {HOME_NAME}"

    return {
        "title": event.title[:256],
        "url": event.url,
        "description": f"**Date:** {event.date_text}\n**Location:** {event.location}\n**Distance:** {distance}\n**Source:** {event.source}",
        "color": 3447003,
    }


def post_to_discord(events: List[Event]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set.")

    if not events:
        payload = {"content": "No new nearby combat robotics events found today."}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30).raise_for_status()
        return

    for event in events[:10]:
        payload = {
            "username": "Combat Event Tracker",
            "content": "New nearby combat robotics event found:",
            "embeds": [discord_embed(event)],
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30).raise_for_status()


def collect_events() -> List[Event]:
    all_events = []

    for source in SOURCES:
        html = fetch(source["url"])
        if source["type"] == "rce":
            parsed = parse_rce(html, source["url"])
        elif source["type"] == "buildersdb":
            parsed = parse_buildersdb(html, source["url"])
        else:
            parsed = []
        all_events.extend(parsed)

    kept = []
    seen = set()

    for event in all_events:
        if keep_event(event) and event.event_id not in seen:
            seen.add(event.event_id)
            kept.append(event)

    kept.sort(key=lambda e: (e.distance_miles if e.distance_miles is not None else 99999, e.date_text))
    return kept


def main():
    posted = load_posted_ids()
    events = collect_events()
    new_events = [event for event in events if event.event_id not in posted]

    print(f"Found {len(events)} nearby events, {len(new_events)} new.")

    post_to_discord(new_events)

    for event in new_events:
        posted.add(event.event_id)
    save_posted_ids(posted)

    # Also save a human-readable snapshot for officers.
    with open("latest_events.json", "w", encoding="utf-8") as f:
        json.dump([asdict(e) | {"event_id": e.event_id} for e in events], f, indent=2)


if __name__ == "__main__":
    main()
