from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

USER_AGENT = "GlobalArtsCultureCalendar/1.0 (+https://github.com/k10sj02/global-arts-culture-calendar)"
UTC = ZoneInfo("UTC")
REGION_TZ = {"us": "America/New_York", "europe": "Europe/London", "mexico": "America/Mexico_City"}
MONTHS = {name.casefold(): number for number, name in enumerate(
    "January February March April May June July August September October November December".split(), 1
)}
MONTHS.update({"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12})


@dataclass(frozen=True)
class CultureEvent:
    source: str
    source_id: str
    title: str
    start: str
    end: str | None
    venue: str
    address: str
    city: str
    country: str
    region: str
    category: str
    url: str
    description: str = ""
    status: str = "CONFIRMED"

    @property
    def uid(self) -> str:
        digest = hashlib.sha256(f"{self.source}|{self.source_id}".encode()).hexdigest()[:28]
        return f"{digest}@global-arts-culture"


class Fetcher:
    def __init__(self):
        self.client = httpx.Client(timeout=35, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept-Language": "en,es;q=.8,fr;q=.7"})

    def get(self, url: str) -> str:
        response = self.client.get(url)
        response.raise_for_status()
        return response.text


def _plain(value: str) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def _within(value: str, now: datetime, final_day: date) -> bool:
    return now.date() <= date.fromisoformat(value[:10]) <= final_day


def poetry_project(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    url = "https://www.poetryproject.org/events"
    soup = BeautifulSoup(fetcher.get(url), "html.parser")
    output = []
    for card in soup.select("article.upcomingEvent"):
        link, time = card.select_one("a[href]"), card.select_one("time[datetime]")
        if not link or not time or not _within(time["datetime"], now, final_day):
            continue
        category = _plain(card.select_one(".category").get_text()) if card.select_one(".category") else "Literature"
        location = _plain(card.select_one(".location").get_text()) if card.select_one(".location") else "The Poetry Project"
        event_url = urljoin(url, link["href"])
        output.append(CultureEvent("poetry-project", f"{event_url}-{time['datetime']}", _plain(card.select_one("h2").get_text()), time["datetime"], None,
            location, "131 E 10th St, New York, NY 10003", "New York", "United States", "us", category, event_url))
    if not output:
        raise RuntimeError("No current Poetry Project events found")
    return output


def ica_london(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    url = "https://www.ica.art/calendar"
    soup = BeautifulSoup(fetcher.get(url), "html.parser")
    output, seen = [], set()
    for card in soup.select("div.item"):
        link, title_node, date_node = card.select_one("a[href]"), card.select_one(".title-container"), card.select_one(".date")
        if not link or not title_node or not date_node:
            continue
        event_url = urljoin(url, link["href"])
        if event_url in seen:
            continue
        seen.add(event_url)
        text = _plain(date_node.get_text(" "))
        year_match = re.search(r"20\d{2}", text)
        year = int(year_match.group()) if year_match else now.year
        names = "|".join(MONTHS.keys())
        match = re.search(rf"(\d{{1,2}})(?:\s*[–-]\s*(\d{{1,2}}))?\s+({names})", text, re.I)
        if not match:
            continue
        first, last, month_name = match.groups()
        month = MONTHS[month_name.casefold()]
        start_day = date(year, month, int(first))
        end_day = date(year, month, int(last)) + timedelta(days=1) if last else None
        start_value: str = start_day.isoformat()
        end_value = end_day.isoformat() if end_day else None
        try:
            detail = BeautifulSoup(fetcher.get(event_url), "html.parser")
            exact_date, exact_time = detail.select_one(".performance.future .date"), detail.select_one(".performance.future .time")
            if exact_date and exact_time:
                dm = re.search(rf"(\d{{1,2}})\s+({names})\s+(20\d{{2}})", _plain(exact_date.get_text(" ")), re.I)
                tm = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", _plain(exact_time.get_text(" ")), re.I)
                if dm and tm:
                    day, mon, yr = dm.groups(); hour, minute, suffix = tm.groups()
                    hour = int(hour) % 12 + (12 if suffix.casefold() == "pm" else 0)
                    start_value = datetime(int(yr), MONTHS[mon.casefold()], int(day), hour, int(minute), tzinfo=ZoneInfo("Europe/London")).isoformat()
                    end_value = None
        except Exception:
            pass
        if not _within(start_value, now, final_day) and not (end_value and date.fromisoformat(end_value[:10]) > now.date()):
            continue
        category = _plain(card.select_one(".subhead").get_text()) if card.select_one(".subhead") else "Arts"
        output.append(CultureEvent("ica-london", event_url, _plain(title_node.get_text(" ")), start_value, end_value,
            "Institute of Contemporary Arts", "The Mall, London SW1Y 5AH", "London", "United Kingdom", "europe", category, event_url,
            _plain(card.select_one(".description").get_text(" ")) if card.select_one(".description") else ""))
    if not output:
        raise RuntimeError("No current ICA London events found")
    return output


def _jsonld_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_objects(item)
    elif isinstance(value, dict):
        if value.get("@type") == "Event" or "Event" in (value.get("@type") or []):
            yield value
        for key in ("@graph", "itemListElement", "subEvent"):
            if key in value:
                yield from _jsonld_objects(value[key])


def matadero_madrid(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    listing = "https://www.mataderomadrid.org/en/schedule"
    soup = BeautifulSoup(fetcher.get(listing), "html.parser")
    links = sorted({urljoin(listing, a["href"]) for a in soup.select('a[href*="/en/schedule/"]')})
    output = []
    for event_url in links[:80]:
        detail = BeautifulSoup(fetcher.get(event_url), "html.parser")
        for script in detail.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.string or script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            for item in _jsonld_objects(payload):
                start = str(item.get("startDate", ""))
                if not re.match(r"^20\d{2}-\d{2}-\d{2}", start) or not _within(start, now, final_day):
                    continue
                location = item.get("location") or {}
                if isinstance(location, list):
                    location = location[0] if location else {}
                address = location.get("address", {}) if isinstance(location, dict) else {}
                if isinstance(address, dict):
                    address = ", ".join(filter(None, [address.get("streetAddress"), address.get("addressLocality"), address.get("postalCode")]))
                end = str(item.get("endDate", ""))
                if not re.match(r"^20\d{2}-\d{2}-\d{2}", end):
                    end = None
                output.append(CultureEvent("matadero-madrid", f"{event_url}-{start}", _plain(str(item.get("name") or detail.title.string)), start,
                    end, location.get("name", "Matadero Madrid") if isinstance(location, dict) else "Matadero Madrid",
                    str(address or "Plaza de Legazpi 8, Madrid"), "Madrid", "Spain", "europe", "Multidisciplinary", event_url,
                    _plain(str(item.get("description", "")))[:700]))
    if not output:
        raise RuntimeError("No current Matadero Madrid events found")
    return output


def inbal_mexico(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    listing = "https://inba.gob.mx/cartelera/"
    soup = BeautifulSoup(fetcher.get(listing), "html.parser")
    links = sorted({urljoin(listing, a["href"]) for a in soup.select('a[href*="/actividad/"]')})
    output = []
    pattern = re.compile(r"(?:lun|mar|mi[eé]|jue|vie|s[aá]b|dom)\s+(\d{1,2})\s+([a-záéíóú]{3,})\s*,\s*(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?", re.I)
    for event_url in links[:100]:
        detail = BeautifulSoup(fetcher.get(event_url), "html.parser")
        text = _plain(detail.get_text(" "))
        if "Ciudad de México" not in text:
            continue
        title_meta = detail.select_one('meta[property="og:title"]')
        title = title_meta.get("content") if title_meta else _plain((detail.select_one("h1") or detail.select_one("h2") or detail.title).get_text(" "))
        location_link = detail.select_one('a[href*="maps.google"]')
        venue_text = _plain(location_link.get_text(" ")) if location_link else "INBAL venue"
        for match in pattern.finditer(text):
            day, month_name, hour, minute, end_hour, end_minute = match.groups()
            month = MONTHS.get(unicodedata.normalize("NFKD", month_name.casefold()).encode("ascii", "ignore").decode()[:3])
            if not month:
                continue
            year = now.year + (month < now.month - 2)
            begins = datetime(year, month, int(day), int(hour), int(minute), tzinfo=ZoneInfo("America/Mexico_City"))
            if not now.date() <= begins.date() <= final_day:
                continue
            ends = begins.replace(hour=int(end_hour), minute=int(end_minute)) if end_hour else None
            output.append(CultureEvent("inbal", f"{event_url}-{begins.isoformat()}", _plain(title), begins.isoformat(), ends.isoformat() if ends else None,
                venue_text.split(" Ciudad de México")[0], venue_text, "Mexico City", "Mexico", "mexico", "Arts", event_url))
    if not output:
        raise RuntimeError("No current Mexico City INBAL events found")
    return output


def fringearts(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    listing = "https://fringearts.com/"
    soup = BeautifulSoup(fetcher.get(listing), "html.parser")
    links = sorted({urljoin(listing, a["href"]) for a in soup.select('a[href*="/event/"]')})
    output = []
    names = "|".join(MONTHS.keys())
    for event_url in links:
        detail = BeautifulSoup(fetcher.get(event_url), "html.parser")
        text = _plain(detail.get_text(" "))
        match = re.search(rf"({names})\s+(\d{{1,2}})(?:\s*[–-]\s*(\d{{1,2}}))?,?\s+(20\d{{2}})", text, re.I)
        if not match:
            continue
        month_name, first, last, year = match.groups()
        start_day = date(int(year), MONTHS[month_name.casefold()], int(first))
        if not now.date() <= start_day <= final_day:
            continue
        end_day = date(int(year), MONTHS[month_name.casefold()], int(last)) + timedelta(days=1) if last else None
        title = detail.select_one("h1") or detail.select_one(".entry-title") or detail.title
        output.append(CultureEvent("fringearts", event_url, _plain(title.get_text(" ")), start_day.isoformat(), end_day.isoformat() if end_day else None,
            "FringeArts", "140 N Columbus Blvd, Philadelphia, PA 19106", "Philadelphia", "United States", "us", "Performing Arts", event_url))
    if not output:
        raise RuntimeError("No current FringeArts events found")
    return output


def deduplicate(events: list[CultureEvent]) -> list[CultureEvent]:
    unique = {}
    for event in events:
        title = "".join(c for c in unicodedata.normalize("NFKD", event.title.casefold()) if c.isalnum())
        unique.setdefault((event.city.casefold(), title, event.start[:16]), event)
    return list(unique.values())


def merge_state(current: list[CultureEvent], path: Path, now: datetime, successful: set[str]) -> list[CultureEvent]:
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text()).get("events", {})
        except (OSError, json.JSONDecodeError):
            pass
    merged = {event.uid: {**asdict(event), "missing_runs": 0} for event in current}
    for uid, item in old.items():
        if uid in merged:
            continue
        missing = int(item.get("missing_runs", 0)) + (item.get("source") in successful)
        if date.fromisoformat(item["start"][:10]) >= now.date() - timedelta(days=1):
            merged[uid] = {**item, "missing_runs": missing, "status": "CANCELLED" if missing >= 2 else item.get("status", "CONFIRMED")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated": now.isoformat(), "events": merged}, ensure_ascii=False, indent=2) + "\n")
    return [CultureEvent(**{k: v for k, v in item.items() if k != "missing_runs"}) for item in merged.values()]


def _temporal(value: str):
    return datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)


def build_calendar(events: list[CultureEvent], generated: datetime, name: str) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Global Arts and Culture Calendar//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", name)
    calendar.add("refresh-interval", timedelta(hours=6), parameters={"VALUE": "DURATION"})
    for item in sorted(events, key=lambda value: (value.start, value.city, value.title)):
        component = Event()
        component.add("uid", item.uid)
        component.add("dtstamp", generated.astimezone(UTC))
        component.add("dtstart", _temporal(item.start))
        if item.end:
            component.add("dtend", _temporal(item.end))
        component.add("summary", f"{item.title} — {item.city}")
        component.add("location", ", ".join(filter(None, [item.venue, item.address])))
        component.add("description", "\n".join(filter(None, [item.description, f"Category: {item.category}", f"Original listing: {item.url}"])))
        component.add("url", item.url)
        component.add("status", item.status)
        component.add("categories", f"Arts & Culture,{item.category},{item.city},{item.country}")
        component.add("x-global-culture-source", item.source)
        component.add("x-global-culture-region", item.region)
        calendar.add_component(component)
    return calendar.to_ical()


def main() -> None:
    root = Path.cwd()
    now = datetime.now(ZoneInfo("America/Toronto"))
    final_day = now.date() + timedelta(days=365)
    fetcher = Fetcher()
    loaders = (("poetry-project", poetry_project), ("ica-london", ica_london), ("matadero-madrid", matadero_madrid), ("inbal", inbal_mexico), ("fringearts", fringearts))
    collected, successful, errors, counts = [], set(), {}, {}
    for name, loader in loaders:
        try:
            found = loader(fetcher, now, final_day)
            collected.extend(found); successful.add(name); counts[name] = len(found)
        except Exception as exc:
            errors[name] = str(exc)
    if not collected:
        raise RuntimeError("No current cultural events found; refusing to publish an empty calendar")
    events = merge_state(deduplicate(collected), root / "data/state.json", now, successful)
    public = root / "public"; public.mkdir(exist_ok=True)
    feeds = {"global-culture.ics": (events, "Global Arts & Culture"), "global-culture-us.ics": ([e for e in events if e.region == "us"], "Global Culture — United States"),
             "global-culture-europe.ics": ([e for e in events if e.region == "europe"], "Global Culture — Europe"), "global-culture-mexico.ics": ([e for e in events if e.region == "mexico"], "Global Culture — Mexico")}
    for filename, (items, title) in feeds.items():
        (public / filename).write_bytes(build_calendar(items, now, title))
    status = {"generated": now.isoformat(), "count": sum(e.status != "CANCELLED" for e in events), "cancelled": sum(e.status == "CANCELLED" for e in events), "regions": {r: sum(e.region == r and e.status != "CANCELLED" for e in events) for r in ("us", "europe", "mexico")}, "sources": counts, "errors": errors}
    (public / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(status, ensure_ascii=False))
