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

USER_AGENT = "Mozilla/5.0 (compatible; GlobalArtsCultureCalendar/1.0; +https://github.com/k10sj02/global-arts-culture-calendar)"
UTC = ZoneInfo("UTC")
REGION_TZ = {"us": "America/New_York", "europe": "Europe/London", "mexico": "America/Mexico_City"}
MONTHS = {name.casefold(): number for number, name in enumerate(
    "January February March April May June July August September October November December".split(), 1
)}
MONTHS.update({"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12})
MONTHS.update({"jan": 1, "apr": 4, "aug": 8, "sept": 9, "dec": 12})


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


def chicago_humanities(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    api = "https://api.chicagohumanities.org/api/events/"
    payload = json.loads(fetcher.get(api))
    output = []
    for item in payload:
        start_date = item.get("start_date")
        if not start_date or not now.date() <= date.fromisoformat(start_date) <= final_day:
            continue
        start_time = item.get("start_time") or "00:00:00"
        begins = datetime.fromisoformat(f"{start_date}T{start_time}").replace(tzinfo=ZoneInfo("America/Chicago"))
        ends = None
        if item.get("end_time"):
            ends = datetime.fromisoformat(f"{item.get('end_date') or start_date}T{item['end_time']}").replace(tzinfo=ZoneInfo("America/Chicago"))
        venue = item.get("event_venue_page") or item.get("venue_legacy") or {}
        address = ", ".join(filter(None, [venue.get("address"), venue.get("address_line_2"), venue.get("city"), venue.get("state"), venue.get("zip")]))
        event_url = urljoin("https://www.chicagohumanities.org", item.get("url") or "")
        output.append(CultureEvent("chicago-humanities", str(item["id"]), _plain(item.get("title", "Chicago Humanities event")), begins.isoformat(), ends.isoformat() if ends else None,
            item.get("venue_name") or venue.get("title_primary") or "Chicago Humanities", address, "Chicago", "United States", "us", "Arts & Ideas", event_url,
            _plain(item.get("brief", ""))[:700]))
    if not output:
        raise RuntimeError("No current Chicago Humanities events found")
    return output


def harvard_film_archive(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    base = "https://harvardfilmarchive.org/calendar"
    output, seen = [], set()
    for page in range(1, 8):
        soup = BeautifulSoup(fetcher.get(f"{base}?page={page}"), "html.parser")
        cards = soup.select(".m-calendar__spot--event.event")
        if not cards:
            break
        added = 0
        for card in cards:
            link, time, title = card.select_one("a.event__link[href]"), card.select_one(".event__time time[datetime]"), card.select_one(".event__title")
            if not link or not time or not title:
                continue
            event_url = urljoin(base, link["href"])
            key = f"{event_url}-{time['datetime']}"
            if key in seen:
                continue
            seen.add(key)
            begins = datetime.fromisoformat(time["datetime"]).replace(tzinfo=ZoneInfo("America/New_York"))
            if not now.date() <= begins.date() <= final_day:
                continue
            series = card.select_one(".event__series")
            description = _plain(card.select_one(".event__info").get_text(" ")) if card.select_one(".event__info") else ""
            output.append(CultureEvent("harvard-film-archive", key, _plain(title.get_text(" ")), begins.isoformat(), None,
                "Harvard Film Archive", "24 Quincy St, Cambridge, MA 02138", "Boston/Cambridge", "United States", "us", "Film", event_url,
                " — ".join(filter(None, [_plain(series.get_text(" ")) if series else "", description]))))
            added += 1
        if added == 0:
            break
    if not output:
        raise RuntimeError("No current Harvard Film Archive screenings found")
    return output


def cccb_barcelona(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    url = "https://www.cccb.org/en/calendar"
    soup = BeautifulSoup(fetcher.get(url), "html.parser")
    output = []
    for month_group in soup.select(".mp-component-agenda-list"):
        heading = month_group.find("h2")
        match = re.search(r"([A-Za-z]+)\s+(20\d{2})", _plain(heading.get_text(" ")) if heading else "")
        if not match or match.group(1).casefold() not in MONTHS:
            continue
        month, year = MONTHS[match.group(1).casefold()], int(match.group(2))
        for day_group in month_group.select(".js-agenda-day-card"):
            day_node = day_group.select_one(".agenda-card-date-num")
            if not day_node:
                continue
            for card in day_group.select("li.js-agenda-item"):
                link, title, time_node = card.select_one("a[href]"), card.select_one(".agenda-card-title"), card.select_one(".agenda-card-date-time")
                if not link or not title or not time_node:
                    continue
                tm = re.search(r"(\d{1,2}):(\d{2})", _plain(time_node.get_text(" ")))
                if not tm:
                    continue
                begins = datetime(year, month, int(day_node.get_text(strip=True)), int(tm.group(1)), int(tm.group(2)), tzinfo=ZoneInfo("Europe/Madrid"))
                if not now.date() <= begins.date() <= final_day:
                    continue
                category_node, detail = card.select_one(".agenda-card-pretitle span"), card.select_one(".agenda-card-text")
                event_url = urljoin(url, link["href"])
                output.append(CultureEvent("cccb", f"{event_url}-{begins.isoformat()}", _plain(title.get_text(" ")), begins.isoformat(), None,
                    "Centre de Cultura Contemporània de Barcelona", "Carrer de Montalegre 5, 08001 Barcelona", "Barcelona", "Spain", "europe",
                    _plain(category_node.get_text(" ")) if category_node else "Multidisciplinary", event_url, _plain(detail.get_text(" ")) if detail else ""))
    if not output:
        raise RuntimeError("No current CCCB events found")
    return output


def silent_green_berlin(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    output, seen = [], set()
    cursor = date(now.year, now.month, 1)
    for offset in range(8):
        month = (cursor.month - 1 + offset) % 12 + 1
        year = cursor.year + (cursor.month - 1 + offset) // 12
        url = f"https://www.silent-green.net/programm/{year}/{month}"
        soup = BeautifulSoup(fetcher.get(url), "html.parser")
        for card in soup.select(".eventList-event"):
            link, title = card.select_one(".eventList-event-title a[href]"), card.select_one(".eventList-event-title h3")
            date_node, time_node = card.select_one(".eventList-event-date"), card.select_one(".eventList-event-starttime")
            if not link or not title or not date_node:
                continue
            dm = re.search(r"(\d{1,2})\.(\d{1,2})\.", _plain(date_node.get_text(" ")))
            tm = re.search(r"(\d{1,2}):(\d{2})", _plain(time_node.get_text(" ")) if time_node else "")
            if not dm:
                continue
            begins = datetime(year, int(dm.group(2)), int(dm.group(1)), int(tm.group(1)) if tm else 0, int(tm.group(2)) if tm else 0, tzinfo=ZoneInfo("Europe/Berlin"))
            if not now.date() <= begins.date() <= final_day:
                continue
            event_url = urljoin(url, link["href"])
            key = f"{event_url.split('?')[0]}-{begins.isoformat()}"
            if key in seen:
                continue
            seen.add(key)
            category = card.select_one(".eventList-event-cat")
            location = card.select_one(".eventList-event-location")
            output.append(CultureEvent("silent-green", key, _plain(title.get_text(" ")), begins.isoformat(), None,
                f"silent green — {_plain(location.get_text(' '))}" if location else "silent green", "Gerichtstraße 35, 13347 Berlin", "Berlin", "Germany", "europe",
                _plain(category.get_text(" ")) if category else "Music & Arts", event_url))
    if not output:
        raise RuntimeError("No current silent green events found")
    return output


def _livewhale_arts(fetcher: Fetcher, base: str, source: str, city: str, address: str, timezone: str, now: datetime, final_day: date,
                    group_names: set[str] | None = None) -> list[CultureEvent]:
    url = f"{base}/live/json/events/start_date/today/max/500/response_fields/event_types,location,summary,description,group_title"
    payload = json.loads(fetcher.get(url))
    items = payload if isinstance(payload, list) else payload.get("data", [])
    output = []
    arts_types = {"films", "film", "performing arts", "arts", "exhibits", "music", "theater", "dance", "literature", "readings", "performances, concerts and exhibitions"}
    for item in items:
        event_types = {str(value).strip().casefold() for value in (item.get("event_types") or [])}
        group = _plain(str(item.get("group_title") or ""))
        if group_names:
            relevant = group in group_names or bool(event_types & arts_types)
        else:
            relevant = bool(event_types & arts_types)
        if not relevant or item.get("is_canceled"):
            continue
        starts = item.get("date_iso")
        if not starts or not _within(starts, now, final_day):
            continue
        # Avoid turning every open day of a long-running exhibition into a separate calendar event.
        if item.get("is_all_day") and item.get("repeats"):
            continue
        location = _plain(str(item.get("location") or ""))
        category = next(iter(item.get("event_types") or []), "Arts & Culture")
        output.append(CultureEvent(source, f"{item.get('id')}-{starts}", _plain(item.get("title", "Arts event")), starts,
            item.get("date2_iso"), location or group or city, address if not location else location, city, "United States", "us", category,
            item.get("url") or base, _plain(item.get("summary") or item.get("description") or "")[:700]))
    return output


def brown_arts(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    events = _livewhale_arts(fetcher, "https://events.brown.edu", "brown-arts", "Providence", "Brown University, Providence, RI", "America/New_York", now, final_day,
        {"Brown Arts Institute (BAI)", "Department of Music", "Department of Theatre Arts and Performance Studies", "Library"})
    if not events:
        raise RuntimeError("No current Brown Arts events found")
    return events


def berkeley_arts(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    events = _livewhale_arts(fetcher, "https://events.berkeley.edu", "berkeley-arts", "Berkeley/San Francisco", "UC Berkeley, Berkeley, CA", "America/Los_Angeles", now, final_day,
        {"Berkeley Art Museum & Pacific Film Archive", "Cal Performances", "Department of Art Practice", "Department of Theater, Dance, and Performance Studies"})
    if not events:
        raise RuntimeError("No current UC Berkeley arts events found")
    return events


def gwu_arts(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    output, seen = [], set()
    for page in range(1, 7):
        payload = json.loads(fetcher.get(f"https://calendar.gwu.edu/api/2/events?days=365&pp=100&page={page}"))
        rows = payload.get("events", [])
        if not rows:
            break
        for wrapper in rows:
            item = wrapper.get("event", {})
            types = [value.get("name", "") for value in (item.get("filters", {}).get("event_types") or [])]
            if not {value.casefold() for value in types} & {"arts & culture", "exhibition", "lectures & speakers"}:
                continue
            for instance_wrapper in item.get("event_instances", []):
                instance = instance_wrapper.get("event_instance", {})
                start = instance.get("start")
                if not start or not _within(start, now, final_day):
                    continue
                key = f"{item.get('id')}-{start}"
                if key in seen:
                    continue
                seen.add(key)
                venue = item.get("location_name") or item.get("location") or "George Washington University"
                address = item.get("address") or "Washington, DC"
                output.append(CultureEvent("gwu-arts", key, _plain(item.get("title", "Arts event")), start, instance.get("end"), venue, address,
                    "Washington, DC", "United States", "us", next((t for t in types if t.casefold() in {"arts & culture", "exhibition"}), types[0] if types else "Arts & Culture"),
                    item.get("localist_url") or item.get("url") or "https://calendar.gwu.edu/", _plain(item.get("description", ""))[:700]))
    if not output:
        raise RuntimeError("No current GW arts events found")
    return output


def redcat_los_angeles(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    listing = "https://www.redcat.org/"
    soup = BeautifulSoup(fetcher.get(listing), "html.parser")
    output = []
    for card in soup.select('a.event-card-stacked[href^="/events/"]'):
        title_node, date_node = card.select_one(".title"), card.select_one(".date")
        if not title_node or not date_node:
            continue
        clean_title = card.select_one(".title .pl-6")
        title = _plain((clean_title or title_node).get_text(" "))
        date_text = _plain(date_node.get_text(" "))
        match = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:\s*[–-]\s*(?:([A-Za-z]+)\s*)?(\d{1,2}))?", date_text)
        if not match or match.group(1).casefold() not in MONTHS:
            continue
        first_month, first_day, second_month, second_day = match.groups()
        year = now.year + (MONTHS[first_month.casefold()] < now.month - 2)
        first = date(year, MONTHS[first_month.casefold()], int(first_day))
        last = date(year, MONTHS[(second_month or first_month).casefold()], int(second_day or first_day))
        event_url = urljoin(listing, card["href"])
        detail = BeautifulSoup(fetcher.get(event_url), "html.parser")
        time_label = detail.find("dt", string=lambda value: value and value.strip().casefold() == "time")
        time_text = _plain(time_label.find_next_sibling("dd").get_text(" ")) if time_label and time_label.find_next_sibling("dd") else ""
        tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*PM", time_text, re.I)
        hour = int(tm.group(1)) % 12 + 12 if tm else 0
        minute = int(tm.group(2) or 0) if tm else 0
        category_node = card.select_one(".metier")
        category = _plain(category_node.get_text(" ")) if category_node else "Multidisciplinary"
        if category.casefold() == "art" and last > first:
            if now.date() <= first <= final_day or first <= now.date() <= last:
                output.append(CultureEvent("redcat", event_url, title, first.isoformat(), (last + timedelta(days=1)).isoformat(), "REDCAT",
                    "631 W 2nd St, Los Angeles, CA 90012", "Los Angeles", "United States", "us", category, event_url))
            continue
        for day_offset in range((last - first).days + 1):
            occurrence = first + timedelta(days=day_offset)
            if not now.date() <= occurrence <= final_day:
                continue
            begins = datetime(occurrence.year, occurrence.month, occurrence.day, hour, minute, tzinfo=ZoneInfo("America/Los_Angeles"))
            output.append(CultureEvent("redcat", f"{event_url}-{begins.isoformat()}", title, begins.isoformat(), None, "REDCAT",
                "631 W 2nd St, Los Angeles, CA 90012", "Los Angeles", "United States", "us", category, event_url))
    if not output:
        raise RuntimeError("No current REDCAT events found")
    return output


def northwest_film_forum(fetcher: Fetcher, now: datetime, final_day: date) -> list[CultureEvent]:
    base = "https://nwfilmforum.org/calendar/"
    output, seen = [], set()
    # The venue publishes one server-rendered week at a time. Eighteen weeks covers
    # its practical advance-sales window without repeatedly requesting empty months.
    for week in range(18):
        week_start = now.date() + timedelta(days=7 * week)
        soup = BeautifulSoup(fetcher.get(f"{base}?start={week_start.isoformat()}"), "html.parser")
        for card in soup.select("[data-calendar-item]"):
            start_node = card.select_one('meta[itemprop="startDate"]')
            title_node = card.select_one('meta[itemprop="name"]')
            link = card.select_one("a.calendar__item__link[href]")
            if not start_node or not title_node or not link:
                continue
            raw_start = start_node.get("content", "")
            try:
                begins = datetime.fromisoformat(raw_start).replace(tzinfo=ZoneInfo("America/Los_Angeles"))
            except ValueError:
                continue
            if not now.date() <= begins.date() <= final_day:
                continue
            event_url = urljoin(base, link["href"])
            key = f"{event_url}-{begins.isoformat()}"
            if key in seen:
                continue
            seen.add(key)
            duration_node = card.select_one('meta[itemprop="duration"]')
            duration_match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration_node.get("content", "") if duration_node else "")
            ends = begins + timedelta(hours=int(duration_match.group(1) or 0), minutes=int(duration_match.group(2) or 0)) if duration_match else None
            category = "Film" if card.has_attr("data-type-film") else "Workshop" if card.has_attr("data-type-workshop") else "Event"
            output.append(CultureEvent("northwest-film-forum", key, _plain(title_node.get("content", "Arts event")), begins.isoformat(),
                ends.isoformat() if ends else None, "Northwest Film Forum", "1515 12th Ave, Seattle, WA 98122", "Seattle", "United States", "us",
                category, event_url))
    if not output:
        raise RuntimeError("No current Northwest Film Forum events found")
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
    loaders = (("poetry-project", poetry_project), ("brown-arts", brown_arts), ("chicago-humanities", chicago_humanities),
               ("harvard-film-archive", harvard_film_archive), ("gwu-arts", gwu_arts), ("fringearts", fringearts),
               ("redcat", redcat_los_angeles), ("berkeley-arts", berkeley_arts), ("northwest-film-forum", northwest_film_forum), ("ica-london", ica_london),
               ("matadero-madrid", matadero_madrid), ("cccb", cccb_barcelona), ("silent-green", silent_green_berlin), ("inbal", inbal_mexico))
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
    east_coast = {"New York", "Philadelphia", "Boston/Cambridge", "Providence", "Washington, DC"}
    west_coast = {"Los Angeles", "Berkeley/San Francisco", "Seattle", "Portland"}
    feeds = {"global-culture.ics": (events, "Global Arts & Culture"), "global-culture-us.ics": ([e for e in events if e.region == "us"], "Global Culture — United States"),
             "global-culture-east-coast.ics": ([e for e in events if e.city in east_coast], "Global Culture — East Coast"),
             "global-culture-west-coast.ics": ([e for e in events if e.city in west_coast], "Global Culture — West Coast"),
             "global-culture-europe.ics": ([e for e in events if e.region == "europe"], "Global Culture — Europe"), "global-culture-mexico.ics": ([e for e in events if e.region == "mexico"], "Global Culture — Mexico")}
    for filename, (items, title) in feeds.items():
        (public / filename).write_bytes(build_calendar(items, now, title))
    status = {"generated": now.isoformat(), "count": sum(e.status != "CANCELLED" for e in events), "cancelled": sum(e.status == "CANCELLED" for e in events), "regions": {r: sum(e.region == r and e.status != "CANCELLED" for e in events) for r in ("us", "europe", "mexico")}, "sources": counts, "errors": errors}
    (public / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(status, ensure_ascii=False))
