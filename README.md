# James Blake Berlin ticket watcher

Checks both Berlin (Astra Kulturhaus) dates of the James Blake "Trying Times" tour
on Ticketmaster every 30 minutes via GitHub Actions, and pings Telegram the moment
Ticketmaster's own availability badge for either date changes.

- Oct 15 2026: https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/953422232
- Oct 16 2026: https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/352305340

## How it works

1. [Firecrawl](https://firecrawl.dev) scrapes the **artist tour-dates listing
   page** (`ticketmaster.de/artist/james-blake-tickets/765513`), not the
   individual event pages, and returns clean markdown. If Firecrawl errors or
   returns empty or a block page, [Browserless](https://browserless.io) (real
   headless Chrome) retries the same listing page. Both providers
   are retried three times; Browserless is used only after Firecrawl fails.
2. Gemini (your personal API key) reads that markdown and, for each of the two
   Berlin event ids, extracts Ticketmaster's own per-date status badge (e.g.
   "Wenige oder keine Tickets verfügbar" = few or no tickets available) and
   whether the event is still listed at all.
3. State (badge text + listed flag) is kept in `state/state.json`, committed
   back to the repo each run. A Telegram message is sent only when a badge or
   listed-status **changes** from the previous run, so you get one alert, not
   one every 30 minutes. If all scrapes or the Gemini classification fail, it
   also sends a Telegram failure alert.
4. A second scheduled check opens the public event page in a standard
   Browserless browser session and tests 1 ticket first, then 2 tickets if 1
   succeeds. It stops before checkout and sends its result to Telegram.

### Why the listing page, not the event page

Tested live before shipping this, in this order:

1. Firecrawl scraping the individual event page (e.g. `.../event/.../953422232`)
   got walled by Ticketmaster's bot-detection with basic, `stealth`, and
   `enhanced` proxy modes, a forced fresh scrape (`max_age=0`), and DE
   geo-targeting — every attempt returned Ticketmaster's
   "Your Browsing Activity Has Been Paused" block page.
2. A plain fetch of the same event page got HTTP 401.
3. The **artist listing page** loaded cleanly every time, no block, and
   already carries Ticketmaster's own scarcity badge per date — exactly the
   signal we need, with zero interaction with any anti-bot/CAPTCHA system.

So this watcher only reads the listing page. It never clicks through to an
event page's buy flow, and automating past that on a recurring schedule
isn't something to build, intent aside.

Trade-off to know about: the listing badge is coarser than the live event
page. "Wenige oder keine Tickets verfügbar" doesn't tell you the exact
quantity, and if it ever clears entirely we can't independently confirm
checkout will succeed (queues, per-account limits, and the bot-check itself
can still block a real purchase). This tool tells you *something changed —
go look and buy by hand*; it doesn't buy anything for you.

As of 2026-07-13, both dates show that badge (i.e. limited, not sold out,
not wide open) — seeded into `state/state.json` accordingly.

## One-time setup

### 1. Telegram bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, follow
   prompts. Save the token it gives you (`TELEGRAM_BOT_TOKEN`).
2. Message your new bot once (anything) so it can message you back.
3. Get your chat id: message [@userinfobot](https://t.me/userinfobot) or open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after step 2 and read
   `message.chat.id` (`TELEGRAM_CHAT_ID`).

### 2. Firecrawl API key
Sign up at [firecrawl.dev](https://www.firecrawl.dev), grab the key from the
dashboard (`FIRECRAWL_API_KEY`). One scrape per run (the listing page only) —
free tier covers checking every 30 min comfortably.

### 3. Gemini API key
Grab a key from [Google AI Studio](https://aistudio.google.com/apikey)
(`GEMINI_API_KEY`). Default model is `gemini-flash-latest` (an alias Google
keeps pointed at a current Flash-tier model, so it won't go stale like a
pinned version number would).

### 3b. Browserless API key (optional fallback)
Grab a key from [browserless.io](https://www.browserless.io)
(`BROWSERLESS_API_KEY`). Only used if Firecrawl fails or comes back empty —
leave unset and the watcher sends a Telegram failure alert on a Firecrawl
outage instead of falling back.

Browserless is also required for the scheduled 1-then-2 quantity check. That
check uses the normal Browserless WebSocket endpoint—no stealth route and no
proxy parameters. It uses a 60-second session limit. Add
`BROWSERLESS_API_KEY` as a GitHub Actions secret. Set `BROWSERLESS_ENDPOINT`
only if you need to use another Browserless region supported by your account.

### 4. Push this repo to GitHub
```bash
git init
git add .
git commit -m "feat: James Blake Berlin ticket watcher"
gh repo create james-blake-ticket-watcher --source=. --push
```
Prefer a **public** repo — GitHub Actions minutes are unlimited on public repos.
On a private repo, every-30-min checks (~48 runs/day) will eat into the 2,000
free monthly minutes; switch the cron to hourly (`0 * * * *`) in
`.github/workflows/check-tickets.yml` if you keep it private.

### 5. Add repo secrets
GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository
secret, add all four:
- `FIRECRAWL_API_KEY`
- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `BROWSERLESS_API_KEY` (optional — Firecrawl fallback)

### 6. Test it locally first

The Firecrawl scrape + Gemini classify have already been run live against
real keys during development (that's how the listing-page approach was found
and `state/state.json` was seeded) — but Telegram sending has **not** been
tested yet. Confirm that piece before trusting the cron:

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your real keys, including Telegram
export $(cat .env | xargs)
python ticket_watcher/main.py
```

You should see one `listed=True badge='...'` line per event. If the badge
differs from the previous run in `state/state.json`, you'll get a Telegram
message — worth briefly editing `state/state.json` by hand to a fake stale
value once, just to confirm the alert actually arrives.

Once local run looks right, trigger it once from GitHub too: repo -> Actions
tab -> "Check James Blake Berlin tickets" -> "Run workflow".

## Adjusting the interval

Edit the cron line in `.github/workflows/check-tickets.yml`:
- every 30 min: `*/30 * * * *` (default)
- hourly: `0 * * * *`
- every 15 min: `*/15 * * * *` (GitHub may delay/coalesce under load; 15 min
  is roughly the practical floor for scheduled workflows)

## Notes

- Ticketmaster's listing-page markup can change; if the bot stops finding the
  badge correctly, check `state/state.json` and the Action run logs (they
  print `listed=` + `badge=` for both events every run) and adjust the Gemini
  prompt in `ticket_watcher/main.py` if needed.
- `state/state.json` ships pre-seeded with both dates' real badge text as of
  2026-07-13 so the first scheduled run doesn't fire a stale-comparison alert.
  If you reset the repo much later, re-check the listing page and update the
  seed before your first run.
- This only reads the public artist listing page — no login, no purchase
  automation, no clicking into checkout, no bypassing of CAPTCHA/bot-detection.
