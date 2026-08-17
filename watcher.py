#!/usr/bin/env python3
"""
AMC ticket-drop watcher.

Polls a public AMC showtimes page and emails/texts you the moment a specific
movie + premium format (e.g. "The Odyssey" in IMAX 70mm) shows up with
bookable showtimes. Notifications are sent via Gmail SMTP - to your real email
address, and optionally to your phone's free carrier email-to-SMS gateway
(e.g. 5551234567@txt.att.net) so it also arrives as a text, at no cost.

This ONLY reads the public showtimes page - it does not log in, does not touch
checkout/payment, and does not attempt to purchase tickets. See README.md for
context on why (AMC's Terms of Use prohibit automated purchasing).

Usage:
    python watcher.py                   # run the poller loop forever
    python watcher.py --once            # check one time and print the result, then exit
    python watcher.py --send-test-email # send a test notification to confirm SMTP is wired up

Configuration is done via environment variables - see .env.example.
"""

import os
import re
import sys
import json
import time
import random
import logging
import smtplib
import argparse
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Configuration
#
# TODO(new movie/theatre/format): everything below is driven by environment
# variables, so watching something new is normally just a case of setting new
# env vars (in .env locally, or Railway's Variables tab in prod) - no code
# changes needed. The values below are only the *defaults* used when a var
# isn't set. To find the right values for a new movie/theatre/format combo:
#   1. Open the AMC showtimes page for that theatre on a date the movie you
#      want IS already on sale (any format), e.g.
#      https://www.amctheatres.com/movie-theatres/<market>/<theatre-slug>/showtimes?date=YYYY-MM-DD
#   2. View page source (not devtools-rendered DOM - use curl or "view source")
#      and search for `-attributes"`. You'll find ids shaped like:
#      id="<movie-slug>-<theatre-slug>-<format-key>-0-attributes"
#   3. Pull MOVIE_SLUG, THEATRE_SLUG, and FORMAT_KEY straight out of that id.
#      FORMAT_KEY is whatever appears right before "-0-attributes" (e.g.
#      imax70mm, imax, dolbycinemaatamcprime, 70mm, laser, standard).
# --------------------------------------------------------------------------

# TODO: change to the target theatre's showtimes URL + target date.
THEATRE_URL = os.environ.get(
    "THEATRE_URL",
    "https://www.amctheatres.com/movie-theatres/new-york-city/amc-lincoln-square-13/showtimes?date=2026-09-14",
)
# TODO: change to the new movie's slug (see lookup steps above).
MOVIE_SLUG = os.environ.get("MOVIE_SLUG", "the-odyssey-76238")
# TODO: change to the new theatre's slug (must match THEATRE_URL's theatre).
THEATRE_SLUG = os.environ.get("THEATRE_SLUG", "amc-lincoln-square-13")
# TODO: change to the format you want to watch for, e.g. imax70mm, imax, 70mm.
FORMAT_KEY = os.environ.get("FORMAT_KEY", "imax70mm")  # matches AMC's premium-offering value, e.g. imax70mm, imax, 70mm
# TODO: cosmetic only - just used in log lines and the notification text.
MOVIE_DISPLAY_NAME = os.environ.get("MOVIE_DISPLAY_NAME", "The Odyssey")
# TODO: cosmetic only - just used in log lines and the notification text.
FORMAT_DISPLAY_NAME = os.environ.get("FORMAT_DISPLAY_NAME", "IMAX 70mm")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "45"))
POLL_JITTER_SECONDS = int(os.environ.get("POLL_JITTER_SECONDS", "15"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "20"))

RENOTIFY_INTERVAL_SECONDS = int(os.environ.get("RENOTIFY_INTERVAL_SECONDS", "900"))  # 15 min
MAX_RENOTIFY = int(os.environ.get("MAX_RENOTIFY", "6"))

FAILURE_ALERT_THRESHOLD = int(os.environ.get("FAILURE_ALERT_THRESHOLD", "15"))
FAILURE_ALERT_COOLDOWN_SECONDS = int(os.environ.get("FAILURE_ALERT_COOLDOWN_SECONDS", "7200"))  # 2 hours

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# --- Notifications (Gmail SMTP) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
# Comma-separated list of destination addresses. Include both a real email and
# a carrier email-to-SMS gateway address (e.g. 6507399807@txt.att.net) to get
# a free text-like alert alongside the email.
NOTIFY_EMAILS = [
    e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("amc-watcher")


# --------------------------------------------------------------------------
# State persistence (so restarts don't cause duplicate/missing notifications)
# --------------------------------------------------------------------------

def load_state():
    default = {
        "notified": False,
        "notify_count": 0,
        "last_notified_ts": 0,
        "consecutive_failures": 0,
        "last_failure_alert_ts": 0,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                default.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read state file, starting fresh")
    return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Fetching + parsing the AMC showtimes page
# --------------------------------------------------------------------------

def fetch_page(session):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = session.get(THEATRE_URL, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def extract_times_near(html, idx, window=9000):
    """Best-effort extraction of showtime strings following position `idx` in the
    rendered HTML. Purely cosmetic for the SMS body - detection itself does not
    depend on this succeeding."""
    snippet = html[idx: idx + window]
    times = re.findall(r'<time dateTime="[^"]*">(\d{1,2}:\d{2})<!-- -->(am|pm)</time>', snippet)
    almost_full = snippet.count("Almost Full")
    return [f"{h}{ap}" for h, ap in times][:12], almost_full


def check_availability(html):
    """Returns dict: available (bool), times (list[str]), almost_full_count (int).

    Detection looks for the literal rendered `id="<movie>-<theatre>-<format>..."`
    attribute (e.g. id="the-odyssey-76238-amc-lincoln-square-13-imax70mm-0") which
    AMC only emits in the server-rendered HTML once that format's showtimes exist
    for the selected date. This deliberately avoids matching the same string inside
    the page's embedded Next.js hydration/flight-data blob (which contains
    JSON-escaped `\\"id\\":...` text and can reference a movie/format even when no
    showtimes are actually on sale for this date).

    TODO(watching multiple formats / "any format" instead of one specific format):
    this function only checks a single MOVIE_SLUG + THEATRE_SLUG + FORMAT_KEY
    combo. If you want to alert on e.g. "IMAX 70mm OR regular 70mm", change the
    `marker` line below to check several FORMAT_KEY values (e.g. loop over a
    list from a new FORMAT_KEYS env var and return available=True if any match),
    or to alert the instant the movie has *any* showtimes at all, just use the
    looser `movie_present` check (further below) as your `available` signal
    instead.
    """
    marker = f'id="{MOVIE_SLUG}-{THEATRE_SLUG}-{FORMAT_KEY}'
    idx = html.find(marker)
    available = idx != -1
    times, almost_full = ([], 0)
    if available:
        times, almost_full = extract_times_near(html, idx)
    # Note: MOVIE_SLUG alone (without the id="..." prefix) can also match unrelated
    # mentions elsewhere on the page, e.g. a "coming soon" promo widget listing every
    # movie at the theatre regardless of date. Requiring the id="<movie>-<theatre>"
    # prefix means the movie actually has a rendered showtimes region for this date,
    # in *some* format (even if not yet the one we're watching for).
    movie_present = f'id="{MOVIE_SLUG}-{THEATRE_SLUG}' in html
    return {
        "available": available,
        "movie_present_at_all": movie_present,
        "times": times,
        "almost_full_count": almost_full,
    }


# --------------------------------------------------------------------------
# Notifications via Gmail SMTP (also reaches carrier email-to-SMS gateways)
# --------------------------------------------------------------------------

def send_notification(subject, body):
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD]) or not NOTIFY_EMAILS:
        log.error(
            "Email is not fully configured - cannot send notification. Subject was: %s", subject
        )
        return False

    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                for to_addr in NOTIFY_EMAILS:
                    msg = MIMEText(body)
                    msg["Subject"] = subject
                    msg["From"] = GMAIL_ADDRESS
                    msg["To"] = to_addr
                    server.sendmail(GMAIL_ADDRESS, [to_addr], msg.as_string())
            log.info("Notification sent to %s: %s", NOTIFY_EMAILS, subject)
            return True
        except (smtplib.SMTPException, OSError) as e:
            log.error("SMTP send failed (attempt %d): %s", attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    return False


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def build_available_message(result):
    times_str = ", ".join(result["times"]) if result["times"] else "see page for times"
    warn = " Some already show Almost Full!" if result["almost_full_count"] else ""
    subject = f"🎬 {FORMAT_DISPLAY_NAME} tickets for {MOVIE_DISPLAY_NAME} are LIVE!"
    body = (
        f"{FORMAT_DISPLAY_NAME} tickets for {MOVIE_DISPLAY_NAME} are LIVE at "
        f"{THEATRE_SLUG}! Times: {times_str}.{warn} Buy now: {THEATRE_URL}"
    )
    return subject, body


def run_once():
    session = requests.Session()
    html = fetch_page(session)
    result = check_availability(html)
    print(json.dumps(result, indent=2))
    return result


def run_loop():
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD]) or not NOTIFY_EMAILS:
        log.warning(
            "Email env vars are not fully set - the watcher will run and log status, "
            "but will NOT be able to send notifications until configured."
        )

    state = load_state()
    session = requests.Session()
    log.info("Watching %s for %s in %s", THEATRE_URL, MOVIE_DISPLAY_NAME, FORMAT_DISPLAY_NAME)
    log.info(
        "Poll interval: %ss (+/- %ss jitter). Already notified: %s",
        POLL_INTERVAL_SECONDS, POLL_JITTER_SECONDS, state["notified"],
    )

    while True:
        loop_start = time.time()
        try:
            html = fetch_page(session)
            result = check_availability(html)
            state["consecutive_failures"] = 0

            if result["available"]:
                log.info(
                    "AVAILABLE - times=%s almost_full=%d",
                    result["times"], result["almost_full_count"],
                )
                now = time.time()
                should_notify = (
                    not state["notified"]
                    or (
                        state["notify_count"] < MAX_RENOTIFY
                        and now - state["last_notified_ts"] >= RENOTIFY_INTERVAL_SECONDS
                    )
                )
                if should_notify:
                    subject, body = build_available_message(result)
                    if send_notification(subject, body):
                        state["notified"] = True
                        state["notify_count"] += 1
                        state["last_notified_ts"] = now
                        save_state(state)
            else:
                status = (
                    "movie listed but format not on sale yet"
                    if result["movie_present_at_all"]
                    else "movie not yet on the showtimes page"
                )
                log.info("Not available (%s)", status)

        except requests.RequestException as e:
            state["consecutive_failures"] += 1
            log.error("Fetch failed (%d consecutive): %s", state["consecutive_failures"], e)
            if (
                state["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD
                and time.time() - state["last_failure_alert_ts"] >= FAILURE_ALERT_COOLDOWN_SECONDS
            ):
                send_notification(
                    "⚠️ AMC ticket watcher is failing",
                    f"The watcher has failed to reach the showtimes page "
                    f"{state['consecutive_failures']} times in a row. Check the logs/host.",
                )
                state["last_failure_alert_ts"] = time.time()

        save_state(state)

        elapsed = time.time() - loop_start
        sleep_for = max(1, POLL_INTERVAL_SECONDS - elapsed) + random.uniform(0, POLL_JITTER_SECONDS)
        time.sleep(sleep_for)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check once, print result, and exit")
    parser.add_argument(
        "--send-test-email", action="store_true", help="Send a test notification and exit"
    )
    args = parser.parse_args()

    if args.send_test_email:
        ok = send_notification(
            "AMC ticket watcher - test",
            f"Test message from your AMC ticket watcher at {datetime.now(timezone.utc).isoformat()}",
        )
        sys.exit(0 if ok else 1)

    if args.once:
        run_once()
        return

    run_loop()


if __name__ == "__main__":
    main()
