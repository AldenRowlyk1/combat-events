import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


HOME_NAME = os.getenv("HOME_NAME", "ERAU Daytona Beach")
HOME_LAT = float(os.getenv("HOME_LAT", "29.1900"))
HOME_LON = float(os.getenv("HOME_LON", "-81.0480"))
RADIUS_MILES = float(os.getenv("RADIUS_MILES", "450"))
MAJOR_EVENT_RADIUS_MILES = float(os.getenv("MAJOR_EVENT_RADIUS_MILES", "1100"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
ROLE_MENTION = os.getenv("ROLE_MENTION", "").strip()
POST_NO_EVENT_MESSAGE = os.getenv("POST_NO_EVENT_MESSAGE", "true").lower() == "true"
POSTED_FILE = "posted_events.json"

SOURCES = [
    {
        "name": "RobotCombatEvents",
        "url": "https://www.robotcombatevents.com/event_map",
        "type": "rce",
        "major": False,
    },
    {
        "name": "BuildersDB",
        "url": "https://www.buildersdb.com/events?show=all",
        "type": "generic",
        "major": False,
    },
    {
        "name": "NHRL",
        "url": "https://www.nhrl.io/events",
        "type": "generic",
        "major": True,
    },
    {
        "name": "Robot Ruckus",
        "url": "https://www.robotruckus.org/",
        "type": "generic",
        "major": True,
    },
]

CITY_COORDS = {
    "daytona beach, fl": (29.2108, -81.0228),
    "orlando, fl": (28.5383, -81.3792),
    "kissimmee, fl": (28.2919, -81.4076),
    "tampa, fl": (27.9506, -82.4572),
    "st. petersburg, fl": (27.7676, -82.6403),
    "jacksonville, fl": (30.3322, -81.6557),
    "jensen beach, fl": (27.2545, -80.2298),
    "miami, fl": (25.7617, -80.1918),
    "west palm beach, fl": (26.7153, -80.0534),
    "fort lauderdale, fl": (26.1224, -80.1373),
    "gainesville, fl": (29.6516, -82.3248),
    "tallahassee, fl": (30.4383, -84.2807),
    "pensacola, fl": (30.4213, -87.2169),
    "atlanta, ga": (33.7490, -84.3880),
    "savannah, ga": (32.0809, -81.0912),
    "columbia, sc": (34.0007, -81.0348),
    "charleston, sc": (32.7765, -79.9311),
    "charlotte, nc": (35.2271, -80.8431),
    "winston-salem, nc": (36.0999, -80.2442),
    "raleigh, nc": (35.7796, -78.6382),
    "birmingham, al": (33.5186, -86.8104),
    "chattanooga, tn": (35.0456, -85.3097),
    "nashville, tn": (36.1627, -86.7816),
    "norwalk, ct": (41.1176, -73.4079),
    "south norwalk, ct": (41.0959, -73.4176),
    "philadelphia, pa": (39.9526, -75.1652),
    "north brunswick township, nj": (40.4520, -74.4829),
    "harrisburg, pa": (40.2732, -76.8867),
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

WEIGHT_CLASS_WORDS = [
    "fairy", "ant", "beetle", "mantis", "hobby", "feather", "sportsman", "plastic", "30lb", "30 lb", "12lb", "12 lb", "3lb", "3 lb", "1lb", "1 lb",
]


@dataclass
class Event:
    title: str
    date_text: str
    location: str
    url: str
    source: str
    major: bool = False
    distance_miles: Optional[float] = None
    classes: str = "Not listed"
    registration: str = "Check event page"

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
    location = re.sub(r"\s+", " ", (location or "").strip())
    location = location.replace(", United States", "")
    location = location.replace(", US", "")
    return location.strip(" ,")


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

    for city_key, coords in CITY_COORDS.items():
        if city_key in loc.lower():
            return coords

    return None


def first_parseable_date(date_text: str) -> Optional[date]:
    try:
        first_date = re.split(r"\s+(?:-|to|through)\s+", date_text.strip(), flags=re.I)[0]
        return date_parser.parse(first_date, fuzzy=True).date()
    except Exception:
        return None


def event_is_futureish(date_text: str) -> bool:
    dt = first_parseable_date(date_text)
    if dt is None:
        return True
    return dt >= date.today()


def distance_for(location: str) -> Optional[float]:
    coords = find_coords(location)
    if not coords:
        return None
    return haversine_miles(HOME_LAT, HOME_LON, coords[0], coords[1])


def keep_event(event: Event) -> bool:
    if not event.title or not event_is_futureish(event.date_text):
        return False

    event.distance_miles = distance_for(event.location)

    radius = MAJOR_EVENT_RADIUS_MILES if event.major else RADIUS_MILES
    if event.distance_miles is not None:
        return event.distance_miles <= radius

    loc_upper = event.location.upper()
    return any(token in loc_upper for token in [" FL", ",FL", "FLORIDA", " GA", " NC", " SC", " AL", " TN"])


def fetch(url: str) -> str:
    headers = {"User-Agent": "ERAU-ASME-Combat-Robotics-Event-Tracker/2.0"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def guess_classes(text: str) -> str:
    found = []
    lower = text.lower()
    for word in WEIGHT_CLASS_WORDS:
        if word in lower and word.replace(" ", "") not in [x.replace(" ", "").lower() for x in found]:
            found.append(word.title().replace("Lb", "lb"))
    return ", ".join(found[:8]) if found else "Not listed"


def guess_registration(text: str) -> str:
    lower = text.lower()
    if "registration closed" in lower or "sold out" in lower:
        return "Likely closed, check page"
    if "register" in lower or "registration" in lower or "sign up" in lower:
        return "Likely open, check page"
    return "Check event page"


def parse_rce(html: str, source_url: str) -> List[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for h3 in soup.find_all("h3"):
        link = h3.find("a")
        if not link:
            continue

        title = link.get_text(" ", strip=True)
        date_text = h3.get_text(" ", strip=True).replace(title, "").replace("\xa0", " ").strip()
        loc_tag = h3.find_next("h4")
        location = loc_tag.get_text(" ", strip=True) if loc_tag else ""
        href = link.get("href", "")
        url = urljoin("https://www.robotcombatevents.com", href) if href else source_url
        nearby_text = " ".join([h3.get_text(" ", strip=True), loc_tag.get_text(" ", strip=True) if loc_tag else ""])

        if title and location:
            events.append(Event(title, date_text or "Date not listed", normalize_location(location), url, "RobotCombatEvents", classes=guess_classes(nearby_text), registration=guess_registration(nearby_text)))

    return events


def event_from_json_ld(data: dict, source: dict) -> Optional[Event]:
    event_type = data.get("@type")
    if isinstance(event_type, list):
        is_event = "Event" in event_type
    else:
        is_event = event_type == "Event"
    if not is_event:
        return None

    title = data.get("name") or "Untitled event"
    start = data.get("startDate") or data.get("startdate") or "Date not listed"
    date_text = start
    try:
        date_text = date_parser.parse(start).strftime("%b %-d, %Y")
    except Exception:
        pass

    location = "Location not listed"
    loc = data.get("location")
    if isinstance(loc, dict):
        address = loc.get("address")
        if isinstance(address, dict):
            city = address.get("addressLocality", "")
            region = address.get("addressRegion", "")
            location = normalize_location(f"{city}, {region}") or loc.get("name", location)
        else:
            location = loc.get("name", location)
    elif isinstance(loc, str):
        location = loc

    url = data.get("url") or source["url"]
    text = json.dumps(data)
    return Event(str(title), str(date_text), normalize_location(location), url, source["name"], major=source.get("major", False), classes=guess_classes(text), registration=guess_registration(text))


def parse_json_ld(soup: BeautifulSoup, source: dict) -> List[Event]:
    events = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = json.loads(script.string or "{}")
        except Exception:
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if isinstance(item, dict) and "@graph" in item:
                items.extend(item.get("@graph", []))
                continue
            if isinstance(item, dict):
                event = event_from_json_ld(item, source)
                if event:
                    events.append(event)
    return events


def parse_generic_cards(html: str, source: dict) -> List[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events = parse_json_ld(soup, source)
    if events:
        return events

    candidates = []
    for tag in soup.find_all(["article", "section", "li", "div"]):
        text = tag.get_text(" ", strip=True)
        if len(text) < 20 or len(text) > 700:
            continue
        if not re.search(r"\b(20\d{2}|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", text, re.I):
            continue
        if not re.search(r"\b(robot|combat|event|ruckus|NHRL|beetle|ant|fairy|hobby|lb)\b", text, re.I):
            continue
        candidates.append(tag)

    seen_texts = set()
    for tag in candidates[:40]:
        text = tag.get_text(" ", strip=True)
        if text in seen_texts:
            continue
        seen_texts.add(text)

        title_tag = tag.find(["h1", "h2", "h3", "h4", "a"])
        title = title_tag.get_text(" ", strip=True) if title_tag else source["name"] + " event"
        link = tag.find("a", href=True)
        url = urljoin(source["url"], link["href"]) if link else source["url"]

        date_match = re.search(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*20\d{2})?(?:\s*(?:-|to|through)\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*20\d{2})?|20\d{2}-\d{2}-\d{2}", text, re.I)
        date_text = date_match.group(0) if date_match else "Date not listed"

        location = "Location not listed"
        loc_match = re.search(r"([A-Z][A-Za-z .'-]+,\s*(?:FL|GA|SC|NC|AL|TN|CT|PA|NJ))", text)
        if loc_match:
            location = loc_match.group(1)

        events.append(Event(title, date_text, normalize_location(location), url, source["name"], major=source.get("major", False), classes=guess_classes(text), registration=guess_registration(text)))

    return events


def parse_manual_events() -> List[Event]:
    raw = os.getenv("MANUAL_EVENTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"MANUAL_EVENTS_JSON could not be parsed: {exc}")
        return []

    events = []
    for item in data:
        events.append(Event(
            title=item.get("title", "Untitled manual event"),
            date_text=item.get("date_text", "Date not listed"),
            location=item.get("location", "Location not listed"),
            url=item.get("url", ""),
            source=item.get("source", "Manual"),
            major=bool(item.get("major", False)),
            classes=item.get("classes", "Not listed"),
            registration=item.get("registration", "Check event page"),
        ))
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
    distance = "Unknown distance"
    if event.distance_miles is not None:
        distance = f"{event.distance_miles:.0f} miles from {HOME_NAME}"

    title = event.title[:256]
    if event.major:
        title = "⭐ " + title

    return {
        "title": title,
        "url": event.url or None,
        "description": "🤖 **New Combat Robotics Event Found**",
        "color": 3447003,
        "fields": [
            {"name": "Event", "value": event.title[:1024], "inline": False},
            {"name": "Date", "value": event.date_text[:1024], "inline": True},
            {"name": "Location", "value": event.location[:1024], "inline": True},
            {"name": "Distance", "value": distance, "inline": True},
            {"name": "Classes", "value": event.classes[:1024], "inline": True},
            {"name": "Registration", "value": event.registration[:1024], "inline": True},
            {"name": "Source", "value": event.source[:1024], "inline": True},
        ],
        "footer": {"text": f"{HOME_NAME} Combat Robotics Event Tracker"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def post_to_discord(events: List[Event]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set.")

    if not events:
        if POST_NO_EVENT_MESSAGE:
            payload = {"username": "Combat Event Tracker", "content": "No new nearby combat robotics events found today."}
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30).raise_for_status()
        return

    for event in events[:10]:
        content = "New nearby combat robotics event found:"
        if ROLE_MENTION:
            content = f"{ROLE_MENTION}\n{content}"
        payload = {"username": "Combat Event Tracker", "content": content, "embeds": [discord_embed(event)]}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30).raise_for_status()


def collect_events() -> List[Event]:
    all_events = []

    for source in SOURCES:
        try:
            html = fetch(source["url"])
            if source["type"] == "rce":
                parsed = parse_rce(html, source["url"])
            else:
                parsed = parse_generic_cards(html, source)
            all_events.extend(parsed)
        except Exception as exc:
            print(f"Could not read {source['name']}: {exc}")

    all_events.extend(parse_manual_events())
    
    kept = []
    seen = set()
    for event in all_events:
        if keep_event(event) and event.event_id not in seen:
            seen.add(event.event_id)
            kept.append(event)

    kept.sort(key=lambda e: (first_parseable_date(e.date_text) or date.max, e.distance_miles if e.distance_miles is not None else 99999, e.title))
    return kept


def main():
    posted = load_posted_ids()
    events = collect_events()
    new_events = [event for event in events if event.event_id not in posted]

    print(f"Found {len(events)} nearby events, {len(new_events)} new.")
    for event in events:
        print(f"- {event.date_text} | {event.title} | {event.location} | {event.distance_miles}")

    post_to_discord(new_events)

    for event in new_events:
        posted.add(event.event_id)
    save_posted_ids(posted)

    with open("latest_events.json", "w", encoding="utf-8") as f:
        json.dump([asdict(e) | {"event_id": e.event_id} for e in events], f, indent=2)


if __name__ == "__main__":
    main()
