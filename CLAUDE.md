# Golf Bot — Project Instructions

## What this is

Python bot that books tee times at three Austin municipal golf courses
(Lions, Roy Kizer, Jimmy Clay) for the upcoming weekend when the city's
Vermont Systems / WebTrac booking site releases them on Monday at 8:00 PM CT.

## How it actually runs

**Local macOS crontab entry on this machine.** Nothing else.

```
58 19 * * 1 > /Users/michaelhsu/golf-bot/booking.log && /usr/bin/python3 -u /Users/michaelhsu/golf-bot/bot.py >> /Users/michaelhsu/golf-bot/booking.log 2>&1
```

The bot launches at 7:58 PM CT (2 minutes before release), hits the
Queue-it waiting room intentionally, waits through it, lands authenticated
right at 8:00 PM, and starts searching.

**This bot is NOT deployed to:**
- Fly.io (old `fly.toml` was deleted in commit `42fc615`)
- AWS Lambda (old `lambda_handler.py` / `trust-policy.json` deleted)
- GitHub Actions (old `.github/workflows/book-golf.yml` deleted)
- Docker (old `Dockerfile` deleted)

If you see references to any of the above in git history, they're dead.
Ignore them.

## Critical invariant: do NOT reload the page during the pre-release wait

`bot.py::wait_until_release_time()` must NOT touch the Playwright page.
It is literally `time.sleep()` and nothing else.

On 2026-04-13 a well-intentioned "keepalive" `page.reload()` loop was added
to refresh the session every 2 minutes during the 15-minute wait for
8:00 PM release. The reload silently landed the session inside the
Queue-it waiting room, and at 8:00 PM the search parsed Queue-it
placeholder DOM and returned zero rows across all three courses. The
bot has no way to book around this because Queue-it isn't detected
inside a reload — only at the start of a `goto()`.

The current design instead:
- Waits without touching the page (`time.sleep` only)
- Calls `navigate_to_search()` for every search URL, which handles
  Queue-it interception and session expiry as a single funnel
- Verifies `is_authenticated()` after the wait and re-logs-in if needed

If you ever feel tempted to add "session refresh" or "keepalive" logic
during the pre-release wait, **don't**. The entire post-mortem is in the
commit message of the Queue-it rewrite commit.

## Running the bot manually

```bash
# Safe end-to-end test — walks the full flow but aborts before completing checkout
python3 bot.py --now --dry-run --players 2

# Run against a live release window (only do this Monday 8 PM CT or the site is empty)
python3 bot.py

# Immediate (no 8pm wait) — for debugging mid-week when slots may be visible
python3 bot.py --now --players 4

# Show the browser window for visual debugging
python3 bot.py --now --dry-run --headful
```

### CLI flags

- `--now` — skip the 8:00 PM wait; run searches immediately
- `--players N` — override `NUM_PLAYERS` from `config.py` (default 4)
- `--max-time N` — total runtime budget in seconds (default 1800)
- `--dry-run` — walks the full booking flow but aborts at `addtocart.html`
  before submitting. Safe to run against the live site.
- `--headful` — show the Firefox window (default is headless)

## Tests

```bash
python3 -m pytest tests/ -q
```

`tests/test_pure.py` covers pure functions (parse_time, is_time_in_range
for both morning and fallback windows, get_time_priority ordering,
get_next_weekend_dates, phantom blacklist tuple shape, course config
integrity, and player-count fallback config). 30 tests.

There are no browser-integration tests — the only way to verify the
Playwright path is `--dry-run` against the live site.

## Configuration

`config.py` holds the static knobs:
- `COURSE_CODES` — Vermont Systems secondarycode -> course name map (Lions > Roy Kizer > Jimmy Clay > Morris Williams)
- `TIME_PRIORITY` — ordered list of preferred tee times (9am > 8am > 10am > 11am > 12pm > 1pm > fallback afternoon)
- `NUM_PLAYERS` — default 4
- `FALLBACK_NUM_PLAYERS` — default 2; if no slots for NUM_PLAYERS, retry with this many. Set to None to disable.
- `MIN_HOUR = 8`, `MAX_HOUR = 13` — primary search window (8am – 1pm inclusive)
- `FALLBACK_MAX_HOUR = 17` — if morning pass finds nothing, widen to 5pm

Secrets live in `.env` (gitignored):

```
GOLF_USERNAME=...
GOLF_PASSWORD=...
# Optional email notifications
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...       # Gmail app password, not account password
NOTIFICATION_EMAIL=...
```

## How booking works

1. `run_booking()` launches Firefox **once** and reuses the same page
   across retries (so Queue-it progress is never thrown away). If the
   page/browser dies mid-session, the outer loop creates a fresh page
   and retries.
2. `run_booking_session()` logs in, waits until 8:00 PM CT, re-verifies
   auth, then attempts Saturday then Sunday.
3. For each day: first try with `NUM_PLAYERS` (4). If no slots found
   across all passes, retry with `FALLBACK_NUM_PLAYERS` (2).
4. `try_book_day()` runs a two-pass search: morning window first
   (`MAX_HOUR=13`), then a fallback pass widening to `FALLBACK_MAX_HOUR=17`.
5. For each (course, round, pass) combination, `search_and_book_course()`
   navigates via `navigate_to_search()` (Queue-it + session-expiry safe,
   loops up to 3 recovery attempts for chained failures) and calls
   `attempt_booking_click()` on slots in priority order.
6. Slots that return "In Use" or "Unavailable" are added to a session-
   scoped phantom blacklist `{(date, course, time)}` so the bot doesn't
   waste time retrying them.
7. Sunday inherits a `exclude_course` hint from Saturday's successful
   booking, so you don't accidentally book the same course both days.

## Sharing the live dashboard with friends

Run `./share_dashboard.sh` from the repo root. It starts the monitor if
needed, spins up a Cloudflare Quick Tunnel, and prints a share-able
`https://<random>.trycloudflare.com` URL. Anyone on the internet with
that URL can view the dashboard. `Ctrl+C` stops both.

- Requires `cloudflared` (install: `brew install cloudflared`)
- URL changes each run (quick tunnels don't persist names)
- For a stable URL, set up a named tunnel with a custom domain

## Debugging a failed run

1. Tail `booking.log` — the per-step labeled logs in `login_once` will
   tell you exactly which step failed.
2. Look for `[queue]`, `[nav]`, `[login]`, `[search]`, `[book]` tags —
   each subsystem prefixes its output so grep works.
3. Check `debug_screenshots/` for timestamped PNGs captured at failure
   points (login failure, navigation exhaustion, no slots found).
4. If the failure is Queue-it related, verify `is_in_queue()` signatures
   still match (Queue-it updates their HTML occasionally — strings like
   "you're in line", "virtual waiting room", "will be entering our site
   soon", and the `queue-it.net` URL substring are the current tripwires).
