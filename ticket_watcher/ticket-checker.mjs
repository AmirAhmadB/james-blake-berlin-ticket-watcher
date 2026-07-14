import puppeteer from 'puppeteer-core';

const EVENT_URL = process.env.EVENT_URL
  ?? 'https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/953422232?language=en-us';
const TICKET_TYPE_TEXT = process.env.TICKET_TYPE_TEXT ?? 'General Admission';
const BROWSERLESS_TOKEN = required('BROWSERLESS_API_KEY');
const TELEGRAM_TOKEN = required('TELEGRAM_BOT_TOKEN');
const TELEGRAM_CHAT_ID = required('TELEGRAM_CHAT_ID');
const BROWSERLESS_ENDPOINT = process.env.BROWSERLESS_ENDPOINT ?? 'wss://production-sfo.browserless.io';
const BROWSERLESS_WS = new URL(BROWSERLESS_ENDPOINT);
BROWSERLESS_WS.searchParams.set('token', BROWSERLESS_TOKEN);
BROWSERLESS_WS.searchParams.set('timeout', '60000');
BROWSERLESS_WS.searchParams.set('blockAds', 'true');
BROWSERLESS_WS.searchParams.set(
  'blockAdsInclude',
  'ublock-filters,easylist,easyprivacy,pgl,ublock-badware,urlhaus-full',
);
const BROWSERLESS_LAUNCH = JSON.stringify({
  args: ['--window-size=1440,1000', '--lang=en-US'],
});
BROWSERLESS_WS.searchParams.set('launch', BROWSERLESS_LAUNCH);

const BLOCK_MARKERS = [
  'your browsing activity has been paused',
  "let's get your identity verified",
  'access denied',
  'captcha',
];
const UNAVAILABLE_MARKERS = [
  "there aren't enough tickets to complete your request",
  'there are not enough tickets to complete your request',
  'sold out',
  'currently unavailable',
];

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

async function sendTelegram(message) {
  const response = await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: TELEGRAM_CHAT_ID, text: message, disable_web_page_preview: false }),
  });
  if (!response.ok) throw new Error(`Telegram returned HTTP ${response.status}`);
}

async function pageText(page) {
  return (await page.locator('body').innerText()).replaceAll(/\s+/g, ' ').trim();
}

function hasMarker(text, markers) {
  const normalized = text.toLowerCase();
  return markers.some((marker) => normalized.includes(marker));
}

async function clickControlNearTicketType(page, ticketTypeText, expectedLabels) {
  const clicked = await page.evaluate(({ ticketTypeText, expectedLabels }) => {
    const normalized = (value) => (value ?? '').replaceAll(/\s+/g, ' ').trim().toLowerCase();
    const wantedType = normalized(ticketTypeText);
    const wantedLabels = expectedLabels.map(normalized);
    const typeElement = [...document.querySelectorAll('body *')].find((element) =>
      normalized(element.textContent).includes(wantedType)
    );
    if (!typeElement) return false;

    // Keep the search inside the ticket type's nearest reasonably-sized section.
    let scope = typeElement;
    while (scope.parentElement && normalized(scope.parentElement.textContent).length < 800) {
      scope = scope.parentElement;
    }
    const controls = [...scope.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')];
    const control = controls.find((element) => {
      const label = normalized([
        element.textContent,
        element.getAttribute('aria-label'),
        element.getAttribute('title'),
        element.getAttribute('value'),
      ].filter(Boolean).join(' '));
      return wantedLabels.some((wanted) => label === wanted || label.includes(wanted));
    });
    if (!control || control.disabled) return false;
    control.click();
    return true;
  }, { ticketTypeText, expectedLabels });
  if (!clicked) throw new Error(`Could not find an enabled quantity control for ${ticketTypeText}`);
}

async function clickFindTickets(page) {
  const clicked = await page.evaluate(() => {
    const normalized = (value) => (value ?? '').replaceAll(/\s+/g, ' ').trim().toLowerCase();
    const button = [...document.querySelectorAll('button, [role="button"], input[type="submit"]')].find((element) => {
      const label = normalized([element.textContent, element.getAttribute('aria-label'), element.value].filter(Boolean).join(' '));
      return label === 'find tickets';
    });
    if (!button || button.disabled) return false;
    button.click();
    return true;
  });
  if (!clicked) throw new Error('Could not find an enabled Find Tickets button');
}

async function checkQuantity(browser, quantity) {
  const page = await browser.newPage();
  try {
    await page.goto(EVENT_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForFunction(
      (ticketTypeText) => document.body.innerText.includes(ticketTypeText),
      { timeout: 15000 },
      TICKET_TYPE_TEXT,
    );

    let text = await pageText(page);
    if (hasMarker(text, BLOCK_MARKERS)) return { status: 'unknown', detail: 'Ticketmaster showed a challenge page.' };
    if (hasMarker(text, UNAVAILABLE_MARKERS)) return { status: 'unavailable', detail: 'Ticketmaster reported no tickets.' };

    for (let index = 0; index < quantity; index += 1) {
      await clickControlNearTicketType(page, TICKET_TYPE_TEXT, ['+', 'increase', 'add', 'increment']);
    }
    const beforeSearch = await pageText(page);
    await clickFindTickets(page);

    // The response panel may update without navigation, so wait for either a
    // known outcome or a URL change. Neither is treated as a purchase action.
    await page.waitForFunction(
      (before) => document.body.innerText !== before.text || location.href !== before.url,
      { timeout: 15000 },
      { text: beforeSearch, url: page.url() },
    ).catch(() => {});

    text = await pageText(page);
    if (hasMarker(text, BLOCK_MARKERS)) return { status: 'unknown', detail: 'Ticketmaster showed a challenge page.' };
    if (hasMarker(text, UNAVAILABLE_MARKERS)) return { status: 'unavailable', detail: 'Ticketmaster could not fulfill that quantity.' };
    return { status: 'available', detail: 'Ticketmaster did not show an unavailable result.' };
  } finally {
    await page.close().catch(() => {});
  }
}

async function main() {
  const browser = await puppeteer.connect({ browserWSEndpoint: BROWSERLESS_WS.toString() });
  try {
    const one = await checkQuantity(browser, 1);
    if (one.status === 'unknown') {
      await sendTelegram(`🎟 James Blake – unable to verify 1 ticket\n${one.detail}\n${EVENT_URL}`);
      return;
    }
    if (one.status === 'unavailable') {
      await sendTelegram(`🎟 James Blake – ❌ 0 tickets available\n${EVENT_URL}`);
      return;
    }

    const two = await checkQuantity(browser, 2);
    if (two.status === 'available') {
      await sendTelegram(`🎟 James Blake – ✅ 2 tickets available\n${EVENT_URL}`);
    } else if (two.status === 'unavailable') {
      await sendTelegram(`🎟 James Blake – ⚠️ Only 1 ticket available\n${EVENT_URL}`);
    } else {
      await sendTelegram(`🎟 James Blake – 1 ticket looked available; unable to verify 2\n${two.detail}\n${EVENT_URL}`);
    }
  } finally {
    await browser.close();
  }
}

main().catch(async (error) => {
  console.error(error);
  try {
    await sendTelegram(`⚠️ James Blake ticket checker failed\n${error.message}\n${EVENT_URL}`);
  } catch (telegramError) {
    console.error('Could not send failure alert:', telegramError);
  }
  process.exitCode = 1;
});
