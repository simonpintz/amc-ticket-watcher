# AMC Ticket Watcher

Polls a public AMC showtimes page and texts you the instant a specific movie +
format (default: **The Odyssey** in **IMAX 70mm** at **AMC Lincoln Square 13**
on **Mon Sept 14, 2026**) shows up with bookable showtimes.

**This does not buy tickets for you.** It only reads the public showtimes page
and sends an SMS. You still click the link and check out yourself. This is a
deliberate choice: AMC's Terms of Use prohibit automated/bot purchasing, and a
real auto-buy bot would need your AMC login and payment info stored in a
script, plus handling AMC's fraud-detection (Kount) and Cloudflare bot-checks
at checkout - both a security risk and likely to get your account flagged or
banned. Fast, reliable notification + you clicking "buy" is the safe version
of this.

## How detection works

AMC's showtimes page (`amctheatres.com/movie-theatres/.../showtimes?date=...`)
is server-rendered - the showtime data is present in the plain HTML response,
no login or headless browser required. When a movie + format is on sale for a
date, the HTML contains an element like:

```html
<ul id="the-odyssey-76238-amc-lincoln-square-13-imax70mm-0-attributes">...
```

`watcher.py` fetches the page with a normal HTTP GET and checks for
`id="<movie-slug>-<theatre-slug>-<format-key>"`. If it's missing, the format
isn't on sale for that date yet. If it's present, it's live, and the watcher
extracts the showtime list for the text message.

One quirk discovered while building this: AMC's Cloudflare-based traffic
manager ("Global Safety Net") transparently 302-redirects first-time visitors
through a `queue.amctheatres.com` token exchange before serving the page. A
plain `requests.Session()` (which follows redirects and keeps cookies) handles
this automatically - no special code needed - but if AMC changes this
behavior and the watcher starts logging fetch failures, that's the first place
to look.

## 1. Run it locally first (recommended)

```bash
cd amc-ticket-watcher
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env with your Twilio info
```

Sanity-check the parser against a date that already has real showtimes
(doesn't send any SMS, just prints what it finds):

```bash
THEATRE_URL="https://www.amctheatres.com/movie-theatres/new-york-city/amc-lincoln-square-13/showtimes?date=2026-08-21" \
  ./venv/bin/python watcher.py --once
```

You should see `"available": true` with a list of times.

Now check the real target date - right now this should print `"available": false`:

```bash
./venv/bin/python watcher.py --once
```

## 2. Set up Twilio (for SMS)

1. Sign up at [twilio.com/try-twilio](https://www.twilio.com/try-twilio) (free trial includes a small credit).
2. In the console, copy your **Account SID** and **Auth Token** into `.env`.
3. Buy a phone number: Phone Numbers → Buy a Number (~$1.15/mo) → put it in `TWILIO_FROM_NUMBER`.
4. Put your own cell number in `NOTIFY_TO_NUMBER` (format: `+15551234567`).
5. **Trial-account limitation:** until you add billing/upgrade, Twilio trial accounts can only text phone numbers you've verified in the console (Phone Numbers → Verified Caller IDs). Verify your own number there, or upgrade the account, before relying on this for the real event.

Test it end-to-end:

```bash
./venv/bin/python watcher.py --send-test-sms
```

You should get a text within a few seconds. Don't move on until this works.

## 3. Run it 24/7 in the cloud

Your computer being asleep/off would silently break this, so run it on a
small always-on host. Two easy options:

### Option A: Railway (simplest)

1. Push this folder to a GitHub repo (or use the Railway CLI to deploy without git — `npm i -g @railway/cli`, then `railway login && railway init && railway up` from this directory).
2. In the Railway dashboard, open the service → **Variables** → paste in everything from your `.env` (except don't commit `.env` itself - it's gitignored).
3. Railway builds the `Dockerfile` and runs it automatically. Check the **Deployments → Logs** tab to confirm you see `Watching https://... Not available (movie not yet on the showtimes page)` messages every ~45-60s.
4. Railway's free monthly credit is generally enough for a lightweight script like this running continuously for the ~1 month until Sept 14.

### Option B: Fly.io

```bash
brew install flyctl   # or see fly.io/docs/hands-on/install-flyctl
fly launch --no-deploy   # answer prompts, it detects the Dockerfile
fly secrets set THEATRE_URL="..." TWILIO_ACCOUNT_SID="..." TWILIO_AUTH_TOKEN="..." TWILIO_FROM_NUMBER="..." NOTIFY_TO_NUMBER="..."
fly deploy
fly logs
```

### Option C: Any cheap VPS (DigitalOcean, Linode, etc.)

```bash
git clone <your repo> && cd amc-ticket-watcher
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in values
nohup ./venv/bin/python watcher.py > watcher.log 2>&1 &
```

(or set it up as a proper `systemd` service so it survives reboots - ask me and I'll write the unit file.)

## 4. What happens on Sept 14

The moment `IMAX 70MM Showtimes` appears for The Odyssey at Lincoln Square 13
for that date, you'll get a text like:

> 🎬 IMAX 70mm tickets for The Odyssey are LIVE at amc-lincoln-square-13! Times: 10:00am, 2:00pm, 6:00pm, 10:00pm. Buy now: https://www.amctheatres.com/...

It'll re-send that same text every 15 minutes (up to 6 times) in case you miss
the first one, then stop. Go buy your tickets. Once you have them, stop the
watcher (Railway: pause/delete the service; VPS: `kill` the process).

## 5. Tuning

Everything is an env var (see `.env.example`):
- `POLL_INTERVAL_SECONDS` / `POLL_JITTER_SECONDS` - how often it checks. 45s ± 15s is a reasonable balance of speed vs. not hammering AMC's servers. AMC ticket releases for a specific showtime aren't usually announced to the second, so faster polling mainly helps if you're worried about losing a race to other buyers within the same minute the drop happens - going much below ~20s starts to look bot-like to Cloudflare.
- `MOVIE_SLUG` / `THEATRE_SLUG` / `FORMAT_KEY` - change these to watch a different movie/theatre/format. You can find them by viewing the page source of any AMC showtimes page with that movie+theatre and searching for `-attributes"`.
- `FORMAT_KEY` options seen on this page: `imax70mm` (IMAX 70mm), `imax` (IMAX at AMC), `dolbycinemaatamcprime` (Dolby Cinema), `70mm` (plain 70mm, no IMAX).

## Known limitations / risks

- If AMC changes their page's HTML structure or ID naming, detection can silently break. The watcher already alerts you by SMS if it can't reach the page at all for a while, but it can't detect "the page loaded fine but the format looks different now." Re-run the `--once` sanity check against a live on-sale date occasionally (e.g. weekly) to confirm the parser still works.
- Polling from a cloud server IP (rather than a residential IP) is generally more likely to draw Cloudflare/bot-management scrutiny over time than requests from your home connection, even though it worked fine in testing here. If you start seeing repeated fetch failures/timeouts in the logs, try increasing `POLL_INTERVAL_SECONDS` first.
- This is for personal use to avoid missing an on-sale moment - please don't turn this into a resale/scalping tool.
