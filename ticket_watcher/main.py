import json
import logging
import os
import sys
import time
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


BROWSERLESS_CONTENT_URL = "https://production-sfo.browserless.io/content"
SCRAPE_ATTEMPTS = 3
BLOCK_PAGE_MARKERS = (
    "your browsing activity has been paused",
    "let's get your identity verified",
    "access denied",
)


def scrape_listing(firecrawl: Firecrawl, url: str) -> str:
    """Fetch the public listing once with Firecrawl's rendered scraper."""
    result = firecrawl.scrape(
        url,
        formats=["markdown"],
        wait_for=4000,
        only_main_content=False,
        max_age=0,
        timeout=60000,
        location={"country": "DE", "languages": ["de-DE"]},
        proxy="auto",
    )
    markdown = getattr(result, "markdown", None)
    if markdown is None and isinstance(result, dict):
        markdown = result.get("markdown")
    return validate_listing_content(markdown or "", "Firecrawl")


def scrape_listing_browserless(api_key: str, url: str) -> str:
    """Fallback scraper: real headless Chrome via Browserless, used only when
    Firecrawl fails or returns empty content. Returns rendered HTML (not
    markdown) - Gemini is prompted generically enough to read either.

    The fallback is for normal provider/network failures only. It does not
    interact with Ticketmaster's ticket picker, queues, or checkout."""
    resp = requests.post(
        BROWSERLESS_CONTENT_URL,
        params={
            "token": api_key,
        },
        json={
            "url": url,
            "gotoOptions": {"waitUntil": "networkidle2", "timeout": 30000},
            "waitForTimeout": 4000,
            "bestAttempt": True,
            "rejectResourceTypes": ["image", "media", "font"],
        },
        timeout=90,
    )
    resp.raise_for_status()
    target_status = int(resp.headers.get("X-Response-Code", "200"))
    if target_status >= 400:
        status_text = resp.headers.get("X-Response-Status", "unknown error")
        raise RuntimeError(f"Browserless reached Ticketmaster but it returned {target_status} {status_text}")
    return validate_listing_content(resp.text, "Browserless")


def validate_listing_content(content: str, source: str) -> str:
    """Reject empty responses and known block pages before Gemini classifies them."""
    normalized = content.strip()
    if not normalized:
        raise RuntimeError(f"{source} returned empty content")
    lowered = normalized.lower()
    if any(marker in lowered for marker in BLOCK_PAGE_MARKERS):
        raise RuntimeError(f"{source} returned a Ticketmaster block page")
    return normalized


def scrape_with_retries(scrape, source: str) -> str:
    """Retry short-lived provider failures before trying the next provider."""
    last_error: Exception | None = None
    for attempt in range(1, SCRAPE_ATTEMPTS + 1):
        try:
            return scrape()
        except Exception as exc:
            last_error = exc
            if attempt == SCRAPE_ATTEMPTS:
                break
            delay = attempt * 2
            logger.warning("%s attempt %s/%s failed: %s; retrying in %ss", source, attempt, SCRAPE_ATTEMPTS, exc, delay)
            time.sleep(delay)
    raise RuntimeError(f"{source} failed after {SCRAPE_ATTEMPTS} attempts: {last_error}")


def ask_gemini(client: genai.Client, model: str, markdown: str) -> dict:
    events_desc = "\n".join(f"- {e.id}: {e.url}" for e in EVENTS)
    prompt = f"""This is the markdown or HTML of a Ticketmaster.de artist tour-dates listing page.

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


def send_failure_alert(token: str, chat_id: str, detail: str) -> None:
    """Best-effort alert; do not hide the original failure if Telegram is down."""
    try:
        send_telegram(token, chat_id, f"⚠️ James Blake ticket watcher failed\n{detail}")
    except Exception as exc:
        logger.error("Could not send the Telegram failure alert: %s", exc)


def main() -> None:
    firecrawl_key = os.environ["FIRECRAWL_API_KEY"]
    gemini_key = os.environ["GEMINI_API_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    browserless_key = os.environ.get("BROWSERLESS_API_KEY", "")

    firecrawl = Firecrawl(api_key=firecrawl_key)
    gemini_client = genai.Client(api_key=gemini_key)

    state = load_state()
    now = datetime.now(timezone.utc).isoformat()

    try:
        markdown = scrape_with_retries(
            lambda: scrape_listing(firecrawl, ARTIST_URL), "Firecrawl"
        )
        source = "Firecrawl"
    except Exception as firecrawl_exc:
        if not browserless_key:
            detail = f"Firecrawl failed and no Browserless key is configured: {firecrawl_exc}"
            logger.error("ERROR: %s", detail)
            send_failure_alert(tg_token, tg_chat_id, detail)
            sys.exit(1)
        logger.warning("Firecrawl failed (%s), falling back to Browserless", firecrawl_exc)
        try:
            markdown = scrape_with_retries(
                lambda: scrape_listing_browserless(browserless_key, ARTIST_URL), "Browserless"
            )
            source = "Browserless"
        except Exception as fallback_exc:
            detail = f"Firecrawl and Browserless both failed: {fallback_exc}"
            logger.error("ERROR: %s", detail)
            send_failure_alert(tg_token, tg_chat_id, detail)
            sys.exit(1)

    logger.info("Listing source: %s", source)

    try:
        parsed = ask_gemini(gemini_client, gemini_model, markdown)
    except Exception as exc:
        logger.error("ERROR: %s", exc)
        send_failure_alert(tg_token, tg_chat_id, f"Gemini could not classify the listing: {exc}")
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
