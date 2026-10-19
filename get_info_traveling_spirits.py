
import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "traveling_spirits.json"
EVENTS_PATH = BASE_DIR / "sky_events.json"

API_URL = "https://sky-children-of-the-light.fandom.com/api.php"

PARAMS = {
    "action": "parse",
    "page": "Traveling Spirits",
    "prop": "text",
    "format": "json",
}

EVENT_ID = "traveling_spirit_current"
EVENT_TYPE = "Travelling Spirit"

TBA_NAME_AR = "لم يُعلن بعد"
TBA_NAME_EN = "To Be Announced"
DEFAULT_REALM = "غير محدد"
DEFAULT_AREA = "غير محدد"
DEFAULT_ICON = "🧞‍♂️"
TBA_ICON = "❓"


def parse_wiki_date(date_text: str) -> tuple[str, str]:
    """
    Converts:
        October 22 to October 25, 2026

    Into:
        2026-10-22T07:00:00Z
        2026-10-25T06:59:59Z

    Follows the date-range convention in the requested examples.
    """
    pattern = (
        r"^\s*"
        r"([A-Za-z]+)\s+(\d{1,2})"
        r"\s+to\s+"
        r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})"
        r"\s*$"
    )

    match = re.match(pattern, date_text)

    if not match:
        raise ValueError(
            f"Unsupported Traveling Spirit date: {date_text!r}"
        )

    start_month, start_day, end_month, end_day, end_year = (
        match.groups()
    )

    end_date = datetime.strptime(
        f"{end_month} {end_day} {end_year}",
        "%B %d %Y",
    ).date()

    start_year = int(end_year)

    # Handle a range crossing December/January.
    start_month_number = datetime.strptime(
        start_month, "%B"
    ).month

    if start_month_number > end_date.month:
        start_year -= 1

    start_date = datetime.strptime(
        f"{start_month} {start_day} {start_year}",
        "%B %d %Y",
    ).date()

    if start_date > end_date:
        raise ValueError(
            f"Start date is after end date: {date_text!r}"
        )

    start_iso = f"{start_date.isoformat()}T07:00:00Z"
    end_iso = f"{end_date.isoformat()}T06:59:59Z"

    return start_iso, end_iso


def load_spirit_database() -> dict:
    """Loads translations and location details keyed by English name."""
    if not DATABASE_PATH.exists():
        logger.warning(
            "Spirit database not found: %s", DATABASE_PATH
        )
        return {}

    try:
        with DATABASE_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read traveling_spirits.json")
        return {}

    if not isinstance(data, dict):
        logger.error("Spirit database must be a JSON object.")
        return {}

    return data


async def fetch_traveling_spirit() -> dict:
    """
    Fetches the Wiki infobox and returns the standard eight-field event.
    Raises an exception if the Wiki data cannot safely be interpreted.
    """
    headers = {
        "User-Agent": (
            "SkyEventsBot/1.0 "
            "(Traveling Spirit schedule updater)"
        ),
        "Accept": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:
        async with session.get(
            API_URL,
            params=PARAMS,
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

    if "error" in payload:
        raise RuntimeError(
            f"Wiki API error: {payload['error']}"
        )

    html = (
        payload.get("parse", {})
        .get("text", {})
        .get("*", "")
    )

    if not html:
        raise RuntimeError("Wiki returned empty HTML.")

    soup = BeautifulSoup(html, "html.parser")

    infobox = soup.select_one(
        ".portable-infobox.type-TravelingSpirit"
    )

    if not infobox:
        raise RuntimeError(
            "Traveling Spirit infobox was not found."
        )

    def get_field(source: str) -> str:
        element = infobox.select_one(
            f'[data-source="{source}"]'
        )
        return element.get_text(" ", strip=True) if element else ""

    date_text = get_field("date")

    image = infobox.select_one(
        '[data-source="image"] img'
    ) or infobox.select_one(".pi-image img")

    if image:
        image_name = (
            image.get("data-image-name")
            or image.get("alt")
            or ""
        )
        image_url = (
            image.get("data-src")
            or image.get("src")
            or ""
        )
    else:
        image_name = ""
        image_url = ""

    # Image identity is the primary indicator of the TBA state.
    image_is_tba = (
        "to-be-announced" in image_name.lower()
        or "to-be-announced" in image_url.lower()
        or image_name.strip().lower() == "tba"
    )

    start_iso, end_iso = parse_wiki_date(date_text)

    if image_is_tba:
        return {
            "id": EVENT_ID,
            "type": EVENT_TYPE,
            "name_ar": TBA_NAME_AR,
            "name_en": TBA_NAME_EN,
            "realm": DEFAULT_REALM,
            "area": DEFAULT_AREA,
            "icon": TBA_ICON,
            "start_time_iso": start_iso,
            "end_time_iso": end_iso,
        }

    # When revealed, the infobox title contains the spirit name
    # (verified in the Talented Builder response).
    title_element = infobox.select_one(".pi-title")
    name_en = (
        title_element.get_text(" ", strip=True)
        if title_element
        else ""
    )

    if not name_en or name_en.lower() == "next traveling spirit":
        # Try the Spirit Page link if the Wiki exposes one.
        spirit_link = infobox.select_one(
            '[data-source="spirit_page"] a[href]'
        )
        if spirit_link:
            name_en = spirit_link.get_text(" ", strip=True)

    if not name_en or name_en.lower() == "next traveling spirit":
        raise RuntimeError(
            "The Wiki image is not TBA, but the revealed spirit "
            "name could not be identified. Refusing to overwrite "
            "the existing event with uncertain data."
        )

    database = load_spirit_database()
    spirit_data = database.get(name_en, {})

    if not spirit_data:
        logger.warning(
            "Spirit %r is not in traveling_spirits.json",
            name_en,
        )

    return {
        "id": EVENT_ID,
        "type": EVENT_TYPE,
        "name_ar": spirit_data.get("name_ar", name_en),
        "name_en": name_en,
        "realm": spirit_data.get("realm", DEFAULT_REALM),
        "area": spirit_data.get("area", DEFAULT_AREA),
        "icon": DEFAULT_ICON,
        "start_time_iso": start_iso,
        "end_time_iso": end_iso,
    }


def update_sky_events_file(event: dict) -> None:
    """
    Replaces only traveling_spirit_current inside scheduled_events.
    All unrelated events and top-level settings are preserved.
    """
    if not EVENTS_PATH.exists():
        raise FileNotFoundError(
            f"Events file not found: {EVENTS_PATH}"
        )

    with EVENTS_PATH.open("r", encoding="utf-8") as file:
        data = json.load(file)

    scheduled_events = data.get("scheduled_events")

    if not isinstance(scheduled_events, list):
        raise ValueError(
            "'scheduled_events' must be a JSON array."
        )

    for index, existing in enumerate(scheduled_events):
        if (
            isinstance(existing, dict)
            and existing.get("id") == EVENT_ID
        ):
            scheduled_events[index] = event
            break
    else:
        scheduled_events.append(event)

    # Write atomically to avoid leaving a partially written JSON file.
    temp_path = EVENTS_PATH.with_suffix(".json.tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temp_path.replace(EVENTS_PATH)


async def refresh_traveling_spirit() -> dict:
    """Fetches the latest Wiki data and updates sky_events.json."""
    event = await fetch_traveling_spirit()
    update_sky_events_file(event)

    logger.info(
        "Traveling Spirit updated: %s | %s -> %s",
        event["name_en"],
        event["start_time_iso"],
        event["end_time_iso"],
    )

    return event


async def main():
    logging.basicConfig(level=logging.INFO)

    event = await refresh_traveling_spirit()
    print(json.dumps(event, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
