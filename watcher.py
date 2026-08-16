#!/usr/bin/env python3
"""
AMC ticket-drop watcher.

Polls a public AMC showtimes page and sends you an SMS (via Twilio) the moment
a specific movie + premium format (e.g. "The Odyssey" in IMAX 70mm) shows up
with bookable showtimes.

This ONLY reads the public showtimes page - it does not log in, does not touch
checkout/payment, and does not attempt to purchase tickets. See README.md for
context on why (AMC's Terms of Use prohibit automated purchasing).

Usage:
    python watcher.py                 # run the poller loop forever
    python watcher.py --once          # check one time and print the result, then exit
    python watcher.py --send-test-sms # send a test SMS to confirm Twilio is wired up

Configuration is done via environment variables - see .env.example.
"""

import os
import re
import sys
import json
import time
import random
import logging
import argparse
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

THEATRE_URL = os.environ.get(
    "THEATRE_URL",
    "https://www.amctheatres.com/movie-theatres/new-york-city/amc-lincoln-square-13/showtimes?date=2026-09-14",
)
MOVIE_SLUG = os.environ.get("MOVIE_SLUG", "the-odyssey-76238")
THEATRE_SLUG = os.environ.get("THEATRE_SLUG", "amc-lincoln-square-13")
FORMAT_KEY = os.environ.get("FORMAT_KEY", "imax70mm")  # matches AMC's premium-offering value, e.g. imax70mm, imax, 70mm
MOVIE_DISPLAY_NAME = os.environ.get("MOVIE_DISPLAY_NAME", "The Odyssey")
FORMAT_DISPLAY_NAME = os.environ.get("FORMAT_DISPLAY_NAME", "IMAX 70mm")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "45"))
POLL_JITTER_SECONDS = int(os.environ.get("POLL_JITTER_SECONDS", "15"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "20"))

RENOTIFY_INTERVAL_SECONDS = int(os.environ.get("RENOTIFY_INTERVAL_SECONDS", "900"))  # 15 min
MAX_RENOTIFY = int(os.environ.get("MAX_RENOTIFY", "6"))

FAILURE_ALERT_THRESHOLD = int(os.environ.get("FAILURE_ALERT_THRESHOLD", "15"))
FAILURE_ALERT_COOLDOWN_SECONDS = int(os.environ.get("FAILURE_ALERT_COOLDOWN_SECONDS", "7200"))  # 2 hours

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
NOTIFY_TO_NUMBER = os.environ.get("NOTIFY_TO_NUMBER", "")

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
    """
    marker = f'id="{MOVIE_SLUG}-{THEATRE_SLUG}-{FORMAT_KEY}'
    idx = html.find(marker)
    available = idx != -1
    times, almost_full = ([], 0)
    if available:
        times, almost_full = extract_times_near(html, idx)
    movie_present = MOVIE_SLUG in html
    return {
        "available": available,
        "movie_present_at_all": movie_present,
        "times": times,
        "almost_full_count": almost_full,
    }


# --------------------------------------------------------------------------
# SMS via Twilio REST API (plain HTTP call, no twilio SDK dependency)
# --------------------------------------------------------------------------

def send_sms(body):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, NOTIFY_TO_NUMBER]):
        log.error("Twilio is not fully configured - cannot send SMS. Message was: %s", body)
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {"From": TWILIO_FROM_NUMBER, "To": NOTIFY_TO_NUMBER, "Body": body}

    for attempt in range(3):
        try:
            resp = requests.post(
                url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15
            )
            if resp.status_code in (200, 201):
                log.info("SMS sent: %s", body[:80])
                return True
            log.error("Twilio error %s: %s", resp.status_code, resp.text[:300])
        except requests.RequestException as e:
            log.error("Twilio request failed (attempt %d): %s", attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    return False


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def build_available_message(result):
    times_str = ", ".join(result["times"]) if result["times"] else "see page for times"
    warn = " Some already show Almost Full!" if result["almost_full_count"] else ""
    return (
        f"🎬 {FORMAT_DISPLAY_NAME} tickets for {MOVIE_DISPLAY_NAME} are LIVE at "
        f"{THEATRE_SLUG}! Times: {times_str}.{warn} Buy now: {THEATRE_URL}"
    )


def run_once():
    session = requests.Session()
    html = fetch_page(session)
    result = check_availability(html)
    print(json.dumps(result, indent=2))
    return result


def run_loop():
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, NOTIFY_TO_NUMBER]):
        log.warning(
            "Twilio env vars are not fully set - the watcher will run and log status, "
            "but will NOT be able to send SMS alerts until configured."
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
                    if send_sms(build_available_message(result)):
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
                send_sms(
                    f"⚠️ AMC ticket watcher has failed to reach the showtimes page "
                    f"{state['consecutive_failures']} times in a row. Check the logs/host."
                )
                state["last_failure_alert_ts"] = time.time()

        save_state(state)

        elapsed = time.time() - loop_start
        sleep_for = max(1, POLL_INTERVAL_SECONDS - elapsed) + random.uniform(0, POLL_JITTER_SECONDS)
        time.sleep(sleep_for)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check once, print result, and exit")
    parser.add_argument("--send-test-sms", action="store_true", help="Send a test SMS and exit")
    args = parser.parse_args()

    if args.send_test_sms:
        ok = send_sms(
            f"Test message from your AMC ticket watcher at {datetime.now(timezone.utc).isoformat()}"
        )
        sys.exit(0 if ok else 1)

    if args.once:
        run_once()
        return

    run_loop()


if __name__ == "__main__":
    main()
