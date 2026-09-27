import os
import re
import sys
import json
import time
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_URL = "https://www.freejobalert.com"

# Category label -> page URL. Add/remove categories freely.
CATEGORIES = {
    "🆕 Latest Notification": f"{BASE_URL}/latest-notifications/",
    "🎟️ Admit Card": f"{BASE_URL}/admit-card/",
    "✅ Result": f"{BASE_URL}/exam-results/",
    "🔑 Answer Key": f"{BASE_URL}/answer-key/",
    "📅 Exam Date": f"{BASE_URL}/exam-dates/",
}

STATE_FILE = Path(__file__).parent / "seen.json"
MAX_ITEMS_PER_CATEGORY = 40    # how many links to read per page
MAX_MESSAGES_PER_RUN = 25      # safety cap so a first run doesn't flood the chat
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_MESSAGES = 1.2   # seconds, keeps us well under Telegram rate limits

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("freejobalert-bot")


# --------------------------------------------------------------------------
# State handling
# --------------------------------------------------------------------------

def load_seen():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("seen", []))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting with empty state.", STATE_FILE)
    return set()


def save_seen(seen_urls):
    # keep the file from growing forever
    trimmed = list(seen_urls)[-5000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": trimmed}, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def fetch_category(url):
    """Return a list of (title, link) tuples found on a category page."""
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    seen_on_page = set()

    # Every individual notification on freejobalert.com lives at a URL
    # containing "/articles/", e.g.
    # https://www.freejobalert.com/articles/ibps-rrb-crp-xv-recruitment-2026-...
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/articles/" not in href:
            continue

        full_url = urljoin(BASE_URL, href.split("?")[0].split("#")[0])
        title = a.get_text(strip=True)

        if not title or full_url in seen_on_page:
            continue

        seen_on_page.add(full_url)
        items.append((title, full_url))

        if len(items) >= MAX_ITEMS_PER_CATEGORY:
            break

    return items


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables are not set."
        )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(api_url, data=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        log.error("Telegram API error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()


def format_message(category, title, link):
    return (
        f"<b>{category}</b>\n"
        f"{title}\n"
        f'🔗 <a href="{link}">Open details</a>\n'
        f"Source: freejobalert.com"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    seen = load_seen()
    updated_seen = set(seen)
    sent_count = 0
    total_new = 0

    for category, url in CATEGORIES.items():
        try:
            items = fetch_category(url)
        except requests.RequestException as exc:
            log.error("Failed to fetch %s (%s): %s", category, url, exc)
            continue

        log.info("%s: found %d link(s) on page", category, len(items))

        # Reverse so oldest-of-the-batch is sent first (reads top-to-bottom
        # chronologically in the chat).
        for title, link in reversed(items):
            if link in seen:
                continue

            total_new += 1
            updated_seen.add(link)

            if sent_count >= MAX_MESSAGES_PER_RUN:
                log.warning(
                    "Hit MAX_MESSAGES_PER_RUN (%d); remaining new items will be "
                    "sent on the next scheduled run.",
                    MAX_MESSAGES_PER_RUN,
                )
                continue

            try:
                send_telegram_message(format_message(category, title, link))
                sent_count += 1
                time.sleep(SLEEP_BETWEEN_MESSAGES)
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to send message for %s: %s", link, exc)
                # Don't mark as seen if sending failed, so it gets retried later.
                updated_seen.discard(link)

    save_seen(updated_seen)
    log.info("Done. %d new item(s) found, %d message(s) sent.", total_new, sent_count)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")
        sys.exit(1)
    main()
