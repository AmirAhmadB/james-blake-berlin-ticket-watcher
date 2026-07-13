import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from firecrawl import Firecrawl
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ticket_watcher")

# We watch the artist LISTING page, not the individual event ticket pages.
# The event pages sit behind Ticketmaster's bot-detection layer (confirmed by
# testing: Firecrawl basic/stealth/enhanced proxies and even a plain fetch all
# got walled or 401'd there). The listing page is not gated, loads cleanly,
# and already shows Ticketmaster's own per-date scarcity badge (e.g. "Wenige
# oder keine Tickets verfuegbar"). We track that badge text per date and
# alert on any change - that's the same signal a human casually browsing the
# page would see, no click-through, no bot-detection interaction at all.

ARTIST_URL = "https://www.ticketmaster.de/artist/james-blake-tickets/765513"


@dataclass(frozen=True)
class EventConfig:
    id: str
    label: str
    url: str


EVENTS: tuple[EventConfig, ...] = (
    EventConfig(
        id="953422232",
        label="James Blake - Thu 15 Oct 2026 - Astra Kulturhaus Berlin",
        url="https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/953422232",
    ),
    EventConfig(
        id="352305340",
        label="James Blake - Fri 16 Oct 2026 - Astra Kulturhaus Berlin",
        url="https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/352305340",
    ),
)

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "state.json"
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "listed": {"type": "boolean"},
                    "badge": {"type": "string"},
                },
                "required": ["event_id", "listed", "badge"],
            },
        }
    },
    "required": ["events"],
}


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def scrape_listing(firecrawl: Firecrawl, url: str) -> str:
    result = firecrawl.scrape(
        url,
        formats=["markdown"],
        wait_for=4000,
        only_main_content=False,
        max_age=0,
    )
    markdown = getattr(result, "markdown", None)
    if markdown is None and isinstance(result, dict):
        markdown = result.get("markdown")
    return markdown or ""


def ask_gemini(client: genai.Client, model: str, markdown: str) -> dict:
    events_desc = "\n".join(f"- {e.id}: {e.url}" for e in EVENTS)
    prompt = f"""This is the markdown of a Ticketmaster.de artist tour-dates listing page.

Page content:
---
{markdown[:40000]}
---

For each of these event ids/links, find its entry in the listing and report:
- event_id
- listed: true if the event still appears on the page at all, false if it's
  gone (e.g. removed, cancelled, or no longer listed)
- badge: the exact scarcity/status text shown right next to that event's
  entry (e.g. "Wenige oder keine Tickets verfuegbar"), or "" (empty string)
  if no such badge/warning is shown for that event (i.e. just a plain
  "Tickets" link with no scarcity warning).

Events to look for:
{events_desc}

Respond only with the requested JSON, one object per event id listed above."""

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
        ),
    )
    return json.loads(response.text)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    firecrawl_key = os.environ["FIRECRAWL_API_KEY"]
    gemini_key = os.environ["GEMINI_API_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

    firecrawl = Firecrawl(api_key=firecrawl_key)
    gemini_client = genai.Client(api_key=gemini_key)

    state = load_state()
    now = datetime.now(timezone.utc).isoformat()

    try:
        markdown = scrape_listing(firecrawl, ARTIST_URL)
        parsed = ask_gemini(gemini_client, gemini_model, markdown)
    except Exception as exc:
        logger.error("ERROR: %s", exc)
        sys.exit(1)

    by_id = {row["event_id"]: row for row in parsed.get("events", [])}
    events_by_id = {e.id: e for e in EVENTS}

    for event_id, event in events_by_id.items():
        row = by_id.get(event_id)
        if row is None:
            logger.warning("[%s] not found in Gemini response, skipping", event.label)
            continue

        listed = bool(row.get("listed", True))
        badge = (row.get("badge") or "").strip()
        current = {"listed": listed, "badge": badge}
        prev = state.get(event_id, {})

        logger.info("[%s] listed=%s badge=%r", event.label, listed, badge)

        if prev and (prev.get("listed") != listed or prev.get("badge", "") != badge):
            send_telegram(
                tg_token,
                tg_chat_id,
                (
                    f"\U0001F3AB Status changed!\n{event.label}\n"
                    f"Before: listed={prev.get('listed')} badge={prev.get('badge', '') or '(none)'}\n"
                    f"Now:    listed={listed} badge={badge or '(none)'}\n"
                    f"{event.url}"
                ),
            )

        state[event_id] = {**current, "last_checked": now}

    save_state(state)


if __name__ == "__main__":
    main()
