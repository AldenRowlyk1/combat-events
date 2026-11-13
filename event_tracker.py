"""ERAU combat robotics event alerts: daily discovery, weekly digest, source diagnostics.

Dependencies: requests, beautifulsoup4, python-dateutil (existing requirements.txt).
Webhook token must be stored in GitHub Actions secrets, never in this file.
"""
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

HOME_LAT, HOME_LON = 29.1900, -81.0480
RADIUS_MILES = float(os.getenv('RADIUS_MILES', '450'))
MAJOR_RADIUS_MILES = float(os.getenv('MAJOR_EVENT_RADIUS_MILES', '1100'))
WEBHOOK = os.getenv('DISCORD_WEBHOOK_URL', '')
POST_NO_EVENT_MESSAGE = os.getenv('POST_NO_EVENT_MESSAGE', 'false').lower() == 'true'
FORCE_SUMMARY = os.getenv('FORCE_SUMMARY', 'false').lower() == 'true'
ROLE_MENTION = os.getenv('ROLE_MENTION', '').strip()
STATE_PATH = Path('tracker_state.json')
OLD_STATE_PATH = Path('posted_events.json')
SNAPSHOT_PATH = Path('latest_events.json')
STATUS_PATH = Path('source_status.json')
TODAY = datetime.now(ZoneInfo('America/New_York')).date()

SOURCES = [
    ('Robot Combat Events', 'https://www.robotcombatevents.com/', 'rce', False),
    ('Robot Combat Events (map)', 'https://www.robotcombatevents.com/event_map', 'rce', False),
    ('BuildersDB', 'https://www.buildersdb.com/events?show=all', 'buildersdb', False),
    ('NHRL', 'https://www.nhrl.io/events', 'nhrl', True),
    ('Robot Ruckus', 'https://www.robotruckus.org/', 'ruckus', True),
]

# Straight-line distances are intentionally labeled as such, not driving distance.
# Expand this table as needed; unknown cities are reported instead of guessed.
CITIES = {
    'daytona beach, fl': (29.2108,-81.0228), 'orlando, fl': (28.5383,-81.3792),
    'ocala, fl': (29.1872,-82.1401), 'jacksonville, fl': (30.3322,-81.6557),
    'fort pierce, fl': (27.4467,-80.3256), 'west palm beach, fl': (26.7153,-80.0534),
    'tampa, fl': (27.9506,-82.4572), 'kissimmee, fl': (28.2919,-81.4076),
    'miami, fl': (25.7617,-80.1918), 'fort lauderdale, fl': (26.1224,-80.1378),
    'gainesville, fl': (29.6516,-82.3248), 'tallahassee, fl': (30.4383,-84.2807),
    'pensacola, fl': (30.4213,-87.2169), 'st. petersburg, fl': (27.7676,-82.6403),
    'lakeland, fl': (28.0395,-81.9498), 'melbourne, fl': (28.0836,-80.6081),
    'fort myers, fl': (26.6406,-81.8723), 'sarasota, fl': (27.3364,-82.5307),
    'atlanta, ga': (33.7490,-84.3880), 'savannah, ga': (32.0809,-81.0912),
    'augusta, ga': (33.4735,-82.0105), 'columbus, ga': (32.4610,-84.9877),
    'columbia, sc': (34.0007,-81.0348), 'charleston, sc': (32.7765,-79.9311),
    'greenville, sc': (34.8526,-82.3940), 'charlotte, nc': (35.2271,-80.8431),
    'winston-salem, nc': (36.0999,-80.2442), 'raleigh, nc': (35.7796,-78.6382),
    'hickory, nc': (35.7345,-81.3445), 'asheville, nc': (35.5951,-82.5515),
    'birmingham, al': (33.5186,-86.8104), 'mobile, al': (30.6954,-88.0399),
    'huntsville, al': (34.7304,-86.5861), 'nashville, tn': (36.1627,-86.7816),
    'chattanooga, tn': (35.0456,-85.3097), 'norwalk, ct': (41.1176,-73.4079),
    'south norwalk, ct': (41.0959,-73.4176),
}
STATE_NAMES = {'Florida':'FL', 'Georgia':'GA', 'Alabama':'AL', 'South Carolina':'SC',
               'North Carolina':'NC', 'Tennessee':'TN', 'Connecticut':'CT'}
MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
DATE_REGEX = re.compile(rf'\b(?:{MONTH})\s+\d{{1,2}}(?:,?\s+20\d{{2}})?|\b20\d{{2}}-\d{{2}}-\d{{2}}', re.I)
LOCATION_REGEX = re.compile(r'([A-Z][A-Za-z .\'-]{1,60}),\s*(FL|GA|SC|NC|AL|TN|CT|NJ|PA|VA|KY|MS|LA)\b', re.I)
REG_REGEX = re.compile(r'(Registration\s+(?:closed|open|opens\s+(?:\w+\s+){1,3}\d{1,2},?\s+20\d{2}))', re.I)
WEIGHTS_REGEX = re.compile(r'\b(?:150\s*g|1\s*lb|3\s*lb|12\s*lb|15\s*lb|30\s*lb|60\s*lb|120\s*lb|220\s*lb|Fairyweight|Antweight|Plastic Ant|Beetleweight|Hobbyweight|Featherweight|Sportsman)\b', re.I)

@dataclass
class Event:
    title: str
    date: str
    location: str
    url: str
    source: str
    classes: str = 'Not specified'
    registration: str = 'Check event page'
    major: bool = False
    distance_miles: float | None = None

    @property
    def event_id(self):
        # URL + title/date stabilize IDs across registration status changes.
        canonical = (self.source.lower(), self.url.rstrip('/'), self.title.lower(), self.date)
        return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()[:16]


def distance(lat, lon):
    a, b = math.radians(HOME_LAT), math.radians(lat)
    dp, dl = b-a, math.radians(lon-HOME_LON)
    x = math.sin(dp/2)**2 + math.cos(a)*math.cos(b)*math.sin(dl/2)**2
    return 3958.7613 * 2 * math.atan2(math.sqrt(x), math.sqrt(1-x))


def parse_day(value):
    try:
        return dtparser.parse(value, fuzzy=True, default=datetime(TODAY.year,1,1)).date()
    except (ValueError, TypeError, OverflowError):
        return None


def find_date(text):
    m = DATE_REGEX.search(text)
    if not m:
        return None
    snippet = m.group(0)
    # Prefer explicit year; otherwise use year in nearby text.
    if not re.search(r'20\d{2}', snippet):
        near = text[m.start():m.start()+110]
        year = re.search(r'20\d{2}', near)
        if year:
            snippet += ', ' + year.group(0)
        else:
            return None
    dt = parse_day(snippet)
    return dt.isoformat() if dt else None


def location_from(text):
    for full, abbrev in STATE_NAMES.items():
        text = re.sub(r',\s*' + re.escape(full) + r'\b', ', ' + abbrev, text, flags=re.I)
    # Prefer known city names: prevents 'Science Center Birmingham' mistaken as city.
    lower = text.lower()
    matches = [(lower.rfind(city), city) for city in CITIES if city in lower]
    if matches:
        return max(matches)[1].title().replace(', Fl', ', FL').replace(', Ga', ', GA').replace(', Nc', ', NC').replace(', Sc', ', SC').replace(', Al', ', AL').replace(', Tn', ', TN').replace(', Ct', ', CT')
    m = LOCATION_REGEX.search(text)
    if m:
        return m.group(0)
    return ''


def details_from(text):
    classes = list(dict.fromkeys(m.group(0) for m in WEIGHTS_REGEX.finditer(text)))
    reg = REG_REGEX.search(text)
    registration = reg.group(0).strip() if reg else 'Check event page'
    return ', '.join(classes[:8]) if classes else 'Not specified', registration


def parse_buildersdb(html):
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for a in soup.select('a[href]'):
        text = a.get_text(' ', strip=True)
        if len(text) < 25 or len(text) > 800 or 'Registration:' not in text:
            continue
        day = find_date(text)
        loc = location_from(text)
        if not day or not loc:
            continue
        # Title ends just before city, state, country and date in BuildersDB links.
        loc_start = text.lower().find(loc.lower())
        title = text[:loc_start].strip(' -') if loc_start > 0 else text[:75]
        if not title:
            continue
        classes, registration = details_from(text)
        result.append(Event(title, day, loc, urljoin('https://www.buildersdb.com', a.get('href','')),
                            'BuildersDB', classes, registration))
    return result


def parse_jsonld_events(soup, source, major=False):
    result = []
    def explore(value):
        if isinstance(value, list):
            for v in value: explore(v)
        elif isinstance(value, dict):
            typ = value.get('@type', '')
            if typ == 'Event' or isinstance(typ, list) and 'Event' in typ:
                title, start = value.get('name',''), value.get('startDate','')
                dt = parse_day(start)
                loc = value.get('location') or {}
                if isinstance(loc, dict):
                    addr = loc.get('address') or {}
                    if isinstance(addr, dict):
                        loc = ', '.join(str(x) for x in [addr.get('addressLocality',''), addr.get('addressRegion','')] if x)
                    else: loc = str(addr or loc.get('name',''))
                if title and dt and loc:
                    classes, reg = details_from(json.dumps(value))
                    result.append(Event(str(title),dt.isoformat(),str(loc),str(value.get('url','')),
                                        source,classes,reg,major))
            for v in value.values():
                if isinstance(v, (list,dict)): explore(v)
    for script in soup.find_all('script', type='application/ld+json'):
        try: explore(json.loads(script.string or script.get_text()))
        except (TypeError,ValueError): continue
    return result


def parse_rce(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    result = parse_jsonld_events(soup, 'Robot Combat Events')
    for a in soup.select('a[href]'):
        href = a.get('href','')
        if not re.search(r'/events?/\d+(?:\b|/|\?)', href):
            continue
        title = a.get_text(' ', strip=True)
        node = a
        event_text = ''
        for _ in range(5):
            if not node.parent: break
            node = node.parent
            candidate = node.get_text(' ', strip=True)
            if len(candidate) > 2000: break
            if find_date(candidate) and location_from(candidate):
                event_text = candidate
                break
        if not event_text:
            continue
        if not title or len(title)>140:
            day_match = DATE_REGEX.search(event_text)
            title = event_text[:day_match.start()].strip() if day_match else ''
        if not title or len(title)>180: continue
        day = find_date(event_text)
        loc = location_from(event_text)
        classes, reg = details_from(event_text)
        result.append(Event(title,day,loc,urljoin(base,href),'Robot Combat Events',classes,reg))
    return result


def parse_nhrl(html, base):
    soup = BeautifulSoup(html,'html.parser')
    result = parse_jsonld_events(soup,'NHRL',True)
    # Page format: title followed by 'Sat Dec 05 / 10:00 AM Eastern / House of Havoc, Norwalk CT'.
    for heading in soup.find_all(['h2','h3','h4','h5']):
        title = heading.get_text(' ',strip=True)
        if 'championship' not in title.lower() and 'qualifier' not in title.lower(): continue
        parent = heading.parent
        if not parent: continue
        text = parent.parent.get_text(' ',strip=True)[:700] if parent.parent else parent.get_text(' ',strip=True)
        day = find_date(text)
        loc = location_from(text.replace('Norwalk CT','Norwalk, CT'))
        if day and loc:
            result.append(Event(title,day,loc,base,'NHRL','3 lb, 12 lb, 30 lb','Check event page',True))
    return result


def parse_ruckus(html, base):
    soup=BeautifulSoup(html,'html.parser')
    result=parse_jsonld_events(soup,'Robot Ruckus',True)
    txt=soup.get_text(' ',strip=True)[:5000]
    # Published event date on the homepage (e.g. November 7 & 8, 2026).
    m=re.search(rf'({MONTH})\s+(\d{{1,2}})(?:\s*&\s*\d{{1,2}})?,?\s+(20\d{{2}})',txt,re.I)
    if m:
        date_text=f'{m.group(1)} {m.group(2)}, {m.group(3)}'
        day=parse_day(date_text)
        if day:
            result.append(Event('Robot Ruckus '+m.group(3),day.isoformat(),'Orlando, FL',base,'Robot Ruckus',
                                'Check event page','Check event page',True))
    return result


def load_curated():
    path=Path('curated_events.json')
    if not path.exists(): return []
    try:
        data=json.loads(path.read_text(encoding='utf8'))
        return [Event(str(e['title']),str(e['date']),str(e['location']),str(e['url']),
                      'Curated',str(e.get('classes','Not specified')),
                      str(e.get('registration','Check event page')),
                      bool(e.get('major',False))) for e in data]
    except (ValueError,TypeError,KeyError) as ex:
        raise RuntimeError(f'Invalid curated_events.json: {ex}') from ex


def load_state():
    if STATE_PATH.exists():
        state=json.loads(STATE_PATH.read_text(encoding='utf8'))
        state.setdefault('announced',{})
        return state
    # Backward compatible with original list: keep old IDs to avoid spam.
    ids=json.loads(OLD_STATE_PATH.read_text(encoding='utf8')) if OLD_STATE_PATH.exists() else []
    return {'announced': {str(x): {'registration':''} for x in ids}, 'last_summary':'', 'last_warning':''}


def send(payload):
    if not WEBHOOK: raise RuntimeError('DISCORD_WEBHOOK_URL GitHub secret is missing')
    response=requests.post(WEBHOOK,json=payload,timeout=25)
    response.raise_for_status()


def embed(ev, update=False):
    dist = f'{ev.distance_miles:.0f} miles straight-line from ERAU' if ev.distance_miles is not None else 'Unknown'
    return {'title':('Event updated: ' if update else 'New event: ')+ev.title[:220],
            'url':ev.url or None,
            'color':0x2878CF if not update else 0xF2AA4C,
            'fields':[
                {'name':'Date','value':ev.date,'inline':True},
                {'name':'Location','value':ev.location[:100],'inline':True},
                {'name':'Distance','value':dist,'inline':True},
                {'name':'Weight classes','value':ev.classes[:900],'inline':False},
                {'name':'Registration','value':ev.registration[:250],'inline':True},
                {'name':'Source','value':ev.source,'inline':True},
            ],'footer':{'text':'Verify dates and registration with the event organizer'}}


def fetch(session, url):
    resp=session.get(url,timeout=25)
    resp.raise_for_status()
    return resp.text


def collect():
    session=requests.Session()
    session.headers['User-Agent']='Mozilla/5.0 (compatible; ERAUCombatEventTracker/3.0; event monitoring)'
    sources=[]
    raw=[]
    for name,url,kind,major in SOURCES:
        try:
            html=fetch(session,url)
            fn={'rce':lambda:parse_rce(html,url), 'buildersdb':lambda:parse_buildersdb(html),
                'nhrl':lambda:parse_nhrl(html,url),'ruckus':lambda:parse_ruckus(html,url)}[kind]
            found=fn()
            sources.append({'name':name,'url':url,'parsed':len(found),'status':'ok' if found else 'zero detected'})
            print(f'[SOURCE] {name}: {len(found)} events extracted')
            raw.extend(found)
        except Exception as e:
            sources.append({'name':name,'url':url,'parsed':0,'status':'error','detail':str(e)[:250]})
            print(f'[SOURCE ERROR] {name}: {e}')
    curated=load_curated()
    raw.extend(curated)
    print(f'[SOURCE] Curated: {len(curated)} events loaded')
    skipped={'old':0,'unknown_date':0,'unknown_location':0,'out_of_radius':0}
    unique={}
    for ev in raw:
        dt=parse_day(ev.date)
        if not dt:
            skipped['unknown_date']+=1;continue
        if dt<TODAY:
            skipped['old']+=1;continue
        ev.date=dt.isoformat()
        loc=ev.location.lower().strip()
        coords=CITIES.get(loc)
        if coords is None:
            # Avoid inaccurate distance estimates and false claims from state centroids.
            skipped['unknown_location']+=1;continue
        ev.distance_miles=round(distance(*coords),1)
        if ev.distance_miles>(MAJOR_RADIUS_MILES if ev.major else RADIUS_MILES):
            skipped['out_of_radius']+=1;continue
        # Collapse matches across websites and curated fallback; prefer live sources.
        fingerprint=(re.sub(r'[^a-z0-9]+','',ev.title.lower()),ev.date,ev.location.lower())
        if fingerprint not in unique or unique[fingerprint].source == 'Curated' and ev.source != 'Curated':
            unique[fingerprint]=ev
    result=sorted(unique.values(),key=lambda x:(x.date,x.distance_miles,x.title))
    print(f'[FILTER] {len(raw)} extracted total; {len(result)} eligible; skipped={skipped}')
    return result,sources,skipped


def main():
    events,sources,skipped=collect()
    state=load_state()
    announce=state['announced']
    # Existing posted_events.json hashes are older format, but the weekly summary
    # can still surface events without flooding when the parser begins detecting them.
    new=[e for e in events if e.event_id not in announce]
    updates=[e for e in events if e.event_id in announce and announce[e.event_id].get('registration','')
             and announce[e.event_id]['registration']!=e.registration]
    print(f'[RESULT] {len(events)} nearby events, {len(new)} new, {len(updates)} registration updates')
    for ev in events: print(f'  {ev.date}: {ev.title} | {ev.location} | {ev.distance_miles:.0f} mi | {ev.source}')

    # Keep notifications capped per run, but ONLY mark an event after successful delivery.
    notifications=0
    for ev in (new+updates)[:10]:
        payload={'username':'Combat Event Tracker','content': (ROLE_MENTION+'\n' if ROLE_MENTION else '')+
                 ('Registration information changed' if ev in updates else 'New nearby combat robotics event'),
                 'embeds':[embed(ev,ev in updates)]}
        send(payload)
        announce[ev.event_id]={'registration':ev.registration,'date':ev.date}
        notifications+=1
    # Do not discard the rest; they will post on the next run.

    local_date=TODAY.isoformat()
    # Weekly digest on Mondays; FORCE_SUMMARY=true for a one-time manual test.
    if (TODAY.weekday()==0 or FORCE_SUMMARY) and state.get('last_summary')!=local_date:
        summary=sorted(events,key=lambda e:(e.date,e.distance_miles))[:10]
        if summary:
            lines=[f'**{e.date}** • {e.title} • {e.location} ({e.distance_miles:.0f} mi straight-line)' for e in summary]
            message='**Upcoming combat robotics events near ERAU**\n'+'\n'.join(lines)
        else:
            message='**Weekly combat robotics summary:** No upcoming eligible events found. Check source health in GitHub Actions.'
        send({'username':'Combat Event Tracker','content':message[:1900]})
        state['last_summary']=local_date

    problems=[s for s in sources if s['status']=='error' or
              (s['status']=='zero detected' and s['name'].startswith(('Robot Combat Events','BuildersDB')))]
    # Warn in Discord once per 7 days when a core source fails, not on every run.
    last_warn=parse_day(state.get('last_warning',''))
    if problems and (not last_warn or TODAY-last_warn>=timedelta(days=7)):
        msg='**Event tracker source warning:** '+', '.join(s['name']+' ('+s['status']+')' for s in problems)
        send({'username':'Combat Event Tracker','content':msg[:1900]})
        state['last_warning']=local_date

    if POST_NO_EVENT_MESSAGE and not notifications:
        send({'username':'Combat Event Tracker','content':'No new nearby combat robotics events found today.'})

    # Save JSON after Discord sends; GitHub Actions commits these files.
    state['announced']=announce
    STATE_PATH.write_text(json.dumps(state,indent=2,sort_keys=True)+'\n',encoding='utf8')
    SNAPSHOT_PATH.write_text(json.dumps([asdict(e)|{'event_id':e.event_id} for e in events],indent=2)+'\n',encoding='utf8')
    STATUS_PATH.write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),
        'sources':sources,'skipped':skipped,'eligible':len(events),'notifications':notifications},indent=2)+'\n',encoding='utf8')

if __name__=='__main__':
    main()
