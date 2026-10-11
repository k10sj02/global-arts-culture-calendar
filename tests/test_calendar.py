from datetime import datetime
from zoneinfo import ZoneInfo

from icalendar import Calendar

from global_arts_culture_calendar import CultureEvent, build_calendar, deduplicate


def sample(region="us"):
    return CultureEvent("source", "one", "A Reading", "2026-11-01T19:00:00-05:00", None, "Venue", "Address", "New York", "United States", region, "Literature", "https://example.com/event")


def test_calendar_is_valid_and_keeps_local_time():
    parsed = Calendar.from_ical(build_calendar([sample()], datetime(2026, 10, 10, tzinfo=ZoneInfo("UTC")), "Test"))
    event = next(component for component in parsed.walk() if component.name == "VEVENT")
    assert str(event["SUMMARY"]) == "A Reading — New York"
    assert event.decoded("DTSTART").hour == 19


def test_deduplicate():
    assert len(deduplicate([sample(), sample()])) == 1
