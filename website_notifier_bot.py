"""
Telegram Website Post Notifier Bot (With /seturl, /test, and /status)
---------------------------------------------------------------------
Features:
  - /seturl https://your-website.com : Set or change the website URL in chat
  - /test                            : Send a sample notification of the latest post
  - /status                          : Check current monitored URL and post count
  - Escapes HTML characters (&, <, >) in post titles so Telegram never rejects them.
  - Remembers the exact chat_id where /seturl was sent.
"""

import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# Only TELEGRAM_BOT_TOKEN is needed in GitHub Secrets!
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE").strip()
DEFAULT_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "-5296495311")).strip()

RUN_ONCE = os.getenv("RUN_ONCE", "true").lower() == "true"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
STATE_FILE = Path("seen_posts.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Mobile) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    )
}

IGNORED_PATH_PARTS = (
    "/category/",
    "/tag/",
    "/page/",
    "/author/",
    "/search",
    "/login",
    "/register",
    "/contact",
    "/about",
    "/privacy",
    "/terms",
    "/dmca",
    "/disclaimer",
    "javascript:",
    "mailto:",
)


# ---------------------------------------------------------------------------
# State Persistence (Stores target_url, chat_id, last_update_id, seen_posts)
# ---------------------------------------------------------------------------
def load_state() -> Dict[str, Any]:
    """Load bot state from seen_posts.json (tolerates trailing commas in JSON)."""
    default_state = {
        "target_url": "",
        "chat_id": DEFAULT_CHAT_ID,
        "last_update_id": 0,
        "seen_posts": [],
    }
    if STATE_FILE.exists():
        try:
            content = STATE_FILE.read_text(encoding="utf-8")
            # Remove accidental trailing commas before ] or } if edited by hand
            content = re.sub(r",\s*([\]}])", r"\1", content)
            raw = json.loads(content)
            if isinstance(raw, list):
                default_state["seen_posts"] = raw
                return default_state
            if isinstance(raw, dict):
                return {
                    "target_url": raw.get("target_url", ""),
                    "chat_id": str(raw.get("chat_id") or DEFAULT_CHAT_ID),
                    "last_update_id": int(raw.get("last_update_id", 0)),
                    "seen_posts": list(raw.get("seen_posts", [])),
                }
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("Could not read state file, starting fresh: %s", exc)
    return default_state


def save_state(state: Dict[str, Any]) -> None:
    """Persist bot state to seen_posts.json (keeps latest 500 seen posts)."""
    state["seen_posts"] = list(state.get("seen_posts", []))[-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Telegram API Helpers
# ---------------------------------------------------------------------------
def register_bot_commands() -> None:
    """Register slash commands with Telegram so they appear in the chat menu."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "seturl", "description": "Set website URL to monitor"},
        {"command": "test", "description": "Send a test notification for the latest post"},
        {"command": "status", "description": "Show current monitored website"},
        {"command": "help", "description": "Show bot instructions"},
    ]
    try:
        requests.post(api_url, json={"commands": commands}, timeout=10)
    except Exception as exc:
        logger.debug("Could not register commands: %s", exc)


def send_telegram_message(
    text: str, disable_preview: bool = False, chat_id: str = DEFAULT_CHAT_ID
) -> None:
    """Send an HTML-formatted message to the Telegram chat."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    resp = requests.post(api_url, json=payload, timeout=15)
    if not resp.ok:
        logger.error("Telegram API error (%s): %s", resp.status_code, resp.text)
    resp.raise_for_status()


def format_post_notification(post: Dict[str, str], is_test: bool = False) -> str:
    """Build a safe HTML notification string with escaped special characters."""
    safe_title = html.escape(post["title"])
    safe_url = html.escape(post["url"], quote=True)
    heading = "<b>New Post Added! (Test Alert)</b>" if is_test else "<b>New Post Added!</b>"
    return (
        f"{heading}\n\n"
        f"<b>Title:</b> {safe_title}\n"
        f"<a href=\"{safe_url}\">Open Post</a>"
    )


def normalize_url(url_text: str) -> str:
    """Extract clean URL even if pasted inside Markdown [url](url) brackets."""
    url_text = url_text.strip()
    match = re.search(r"https?://[^\s\]\)]+", url_text)
    if match:
        return match.group(0).rstrip("/")
    url_text = url_text.strip("[]()")
    if not url_text.startswith(("http://", "https://")):
        url_text = "https://" + url_text
    return url_text.rstrip("/")


# ---------------------------------------------------------------------------
# Automatic Post Detection (Tries RSS First -> Smart HTML Fallback)
# ---------------------------------------------------------------------------
def fetch_posts_via_rss(site_url: str) -> List[Dict[str, str]]:
    """Try common RSS feed endpoints for the site."""
    candidate_feeds = [
        f"{site_url.rstrip('/')}/feed/",
        f"{site_url.rstrip('/')}/rss.xml",
        f"{site_url.rstrip('/')}/feed",
    ]
    for rss_url in candidate_feeds:
        try:
            resp = requests.get(rss_url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                continue
            posts = []
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if title and link:
                    posts.append({"id": link, "title": title, "url": link})
            if posts:
                logger.info("Fetched %d posts via RSS (%s)", len(posts), rss_url)
                return posts
        except Exception:
            continue
    return []


def fetch_posts_via_html(site_url: str) -> List[Dict[str, str]]:
    """Smart HTML parser that works on almost any blog/media website."""
    try:
        resp = requests.get(site_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        base_domain = urlparse(resp.url).netloc

        posts: List[Dict[str, str]] = []
        seen_urls = set()

        selectors = [
            "article a[href]",
            ".post a[href]",
            ".thumb a[href]",
            ".entry-title a[href]",
            "figcaption a[href]",
            "h2 a[href]",
            "h3 a[href]",
        ]
        candidate_links = []
        for sel in selectors:
            candidate_links.extend(soup.select(sel))

        if not candidate_links:
            candidate_links = soup.find_all("a", href=True)

        for tag in candidate_links:
            raw_href = tag.get("href", "").strip()
            if not raw_href or raw_href.startswith("#"):
                continue

            full_url = urljoin(resp.url, raw_href)
            parsed = urlparse(full_url)

            if base_domain not in parsed.netloc or parsed.path in ("", "/"):
                continue

            lower_url = full_url.lower()
            if any(part in lower_url for part in IGNORED_PATH_PARTS):
                continue

            title = tag.get_text(" ", strip=True) or tag.get("title", "").strip()
            if len(title) < 6 or full_url in seen_urls:
                continue

            seen_urls.add(full_url)
            posts.append({"id": full_url, "title": title, "url": full_url})

        logger.info("Fetched %d posts via HTML from %s", len(posts), site_url)
        return posts
    except Exception as exc:
        logger.error("HTML fetch failed for %s: %s", site_url, exc)
        return []


def get_latest_posts(site_url: str) -> List[Dict[str, str]]:
    """Automatically try RSS first; if unavailable, use smart HTML parsing."""
    posts = fetch_posts_via_rss(site_url)
    if posts:
        return posts
    return fetch_posts_via_html(site_url)


# ---------------------------------------------------------------------------
# Process In-Chat Commands (/seturl, /test, /status, /help)
# ---------------------------------------------------------------------------
def process_telegram_commands(state: Dict[str, Any]) -> None:
    """Check recent messages in Telegram for /seturl, /test, or /status."""
    register_bot_commands()
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": state["last_update_id"] + 1, "timeout": 5}

    try:
        resp = requests.get(api_url, params=params, timeout=15)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
        logger.info("Telegram getUpdates returned %d new message(s).", len(updates))
    except Exception as exc:
        logger.warning("Could not fetch Telegram updates: %s", exc)
        return

    for update in updates:
        state["last_update_id"] = max(state["last_update_id"], update["update_id"])
        msg = update.get("message") or update.get("channel_post") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        if not text.startswith("/"):
            continue

        if chat_id:
            state["chat_id"] = chat_id

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        reply_target = state.get("chat_id") or DEFAULT_CHAT_ID

        if cmd == "/seturl":
            if not arg:
                send_telegram_message(
                    "Please provide a URL after the command.\n"
                    "Example:\n<code>/seturl https://example.com</code>",
                    disable_preview=True,
                    chat_id=reply_target,
                )
                continue

            new_url = normalize_url(arg)
            state["target_url"] = new_url
            initial_posts = get_latest_posts(new_url)
            state["seen_posts"] = [p["id"] for p in initial_posts]
            save_state(state)

            send_telegram_message(
                f"<b>Website URL Updated!</b>\n\n"
                f"<b>Monitoring:</b> {html.escape(new_url)}\n"
                f"<b>Existing posts saved:</b> {len(initial_posts)}\n"
                f"You will now get notified here whenever a new post is added.",
                disable_preview=True,
                chat_id=reply_target,
            )

        elif cmd == "/test":
            target_url = state.get("target_url", "").strip()
            if not target_url:
                send_telegram_message(
                    "No website URL set yet. Send <code>/seturl https://...</code> first.",
                    disable_preview=True,
                    chat_id=reply_target,
                )
                continue
            posts = get_latest_posts(target_url)
            if posts:
                send_telegram_message(
                    format_post_notification(posts[0], is_test=True),
                    chat_id=reply_target,
                )
            else:
                send_telegram_message(
                    f"Could not find any posts on {html.escape(target_url)}.",
                    disable_preview=True,
                    chat_id=reply_target,
                )

        elif cmd == "/status":
            current = state.get("target_url") or "Not set yet (use /seturl https://...)"
            send_telegram_message(
                f"<b>Bot Status</b>\n\n"
                f"<b>Current URL:</b> {html.escape(current)}\n"
                f"<b>Tracked posts:</b> {len(state.get('seen_posts', []))}",
                disable_preview=True,
                chat_id=reply_target,
            )

        elif cmd in ("/start", "/help"):
            send_telegram_message(
                "<b>Website Notifier Bot Commands</b>\n\n"
                "• <code>/seturl https://website.com</code> — Set or change the website to monitor\n"
                "• <code>/test</code> — Send a sample alert for the latest post\n"
                "• <code>/status</code> — View current website and tracked post count",
                disable_preview=True,
                chat_id=reply_target,
            )


# ---------------------------------------------------------------------------
# Check Website & Send Alerts
# ---------------------------------------------------------------------------
def check_once(state: Dict[str, Any]) -> None:
    """Process any chat commands first, then check the target URL for new posts."""
    process_telegram_commands(state)
    save_state(state)

    target_url = state.get("target_url", "").strip()
    if not target_url:
        logger.info("No target_url set yet. Send '/seturl https://...' in your Telegram group.")
        return

    latest_posts = get_latest_posts(target_url)
    if not latest_posts:
        logger.warning("No posts found on %s", target_url)
        return

    seen_set = set(state.get("seen_posts", []))
    notify_chat_id = state.get("chat_id") or DEFAULT_CHAT_ID

    if not seen_set:
        state["seen_posts"] = [p["id"] for p in latest_posts]
        save_state(state)
        logger.info("Seeded %d existing posts for %s", len(latest_posts), target_url)
        return

    new_posts = [p for p in reversed(latest_posts) if p["id"] not in seen_set]
    logger.info("Found %d new post(s) to notify.", len(new_posts))

    for post in new_posts:
        try:
            send_telegram_message(
                format_post_notification(post, is_test=False),
                chat_id=notify_chat_id,
            )
            seen_set.add(post["id"])
            state["seen_posts"].append(post["id"])
            save_state(state)
            logger.info("Notified new post: %s", post["title"])
            time.sleep(1.5)
        except Exception as exc:
            logger.error("Failed to send alert for %s: %s", post["title"], exc)


def main() -> None:
    state = load_state()
    if RUN_ONCE:
        check_once(state)
    else:
        while True:
            check_once(state)
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
