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

## Standby Watch (cancellation polling)

If you miss the Monday 8PM rush, queue a "standby watch" that polls for
cancellations every 15 minutes. Uses one account to search + book.

### CLI

```bash
# Add a watch for this weekend
python3 standby_bot.py add --day saturday --day sunday --time morning
python3 standby_bot.py add --day saturday --time afternoon --players 2

# "At least 3 people": try 4, fall back to 3, never book fewer
python3 standby_bot.py add --day saturday --time morning --players 4 --min-players 3

# Strict "by 10" watch — caps the morning window at 9:59 AM
python3 standby_bot.py add --day saturday --time morning --max-hour 9

# List active watches
python3 standby_bot.py list

# Cancel a watch
python3 standby_bot.py cancel <id>

# Run one check cycle manually
python3 standby_bot.py check
python3 standby_bot.py check --dry-run --headful
```

### Crontab (every 15 min, Sun + Tue–Sat)

```
*/15 * * * 0,2-6 /usr/bin/python3 -u /Users/michaelhsu/golf-bot/standby_bot.py check >> /Users/michaelhsu/golf-bot/standby.log 2>&1
```

Sunday (day 0) is included so same-day cancellations for a Sunday round
are still caught; Monday is excluded because the main 8 PM bot owns it.

**Cron does not fire while the Mac idle-sleeps** — this machine's
`pmset` system sleep is 1 minute, and on 2026-07-04 that silently ate
several 15-minute check cycles (visible as gaps between "Standby Check"
headers in `standby.log`). While a watch is active, keep the Mac awake
for the life of the watch:

```bash
# e.g. until Sunday midnight
SECS=$(( $(date -j -f "%Y-%m-%d %H:%M:%S" "2026-07-05 23:59:59" +%s) - $(date +%s) ))
nohup caffeinate -i -t $SECS >/dev/null 2>&1 &
```

The 8:51 AM Roy Kizer save on 7/05 came from a cancellation caught at
7:31 PM the night before — evening-before and early-same-morning are
the highest-yield windows, so missed cycles there hurt the most.

### Dashboard

The monitor dashboard (localhost:8111) shows the "Standby Watch" section
with active watches, check counts, and results. You can add/cancel
watches directly from the UI.

### How it works

- `standby_queue.py` manages the watch queue (`standby_queue.json`)
- `standby_bot.py check` reads active watches, logs in, searches all
  courses for each requested day/time, books if found
- Time prefs are minute-precise: `morning` (7:00–11:00 AM), `afternoon`
  (1:00–5:00 PM), `all` (8:00 AM–5:00 PM). Add `--max-hour N` to cap the
  window at N:59 (e.g. `--max-hour 9` = nothing after 9:59 AM)
- Player fallback: if no slots for requested count, retries with
  FALLBACK_NUM_PLAYERS (2). A watch created with `--min-players N` instead
  tries every count from `--players` down to N and never books below the
  floor (e.g. `--players 4 --min-players 3` tries 4 then 3, never 2)
- Watches auto-expire Sunday 11:59 PM (end of the target weekend)
- Old terminal watches are cleaned up after 14 days

## Same-day watch (today_watch.py)

One-off watcher for "find me a tee time TODAY in a precise window" runs —
e.g. tonight 5:30–6:30 PM for 2 players. Unlike standby watches
(weekend-only, hour-granular, cron-driven), this is a single self-looping
process with a minute-precise inclusive window that exits at the end of
the window.

```bash
# Auto-book watch: today, 5:30-6:30 PM, 2 players, check every 15 min.
# caffeinate -i keeps the Mac from idle-sleeping while it runs.
nohup caffeinate -i /usr/bin/python3 -u today_watch.py \
    --start "5:30 PM" --end "6:30 PM" --players 2 >> today_watch.log 2>&1 &

# Immediate one-shot check that only notifies (safe smoke test)
python3 today_watch.py --once --mode notify
```

- Searches all `COURSE_CODES` courses, 18-hole then 9-hole listings
  (`--holes "18,9"`) — evening twilight slots are often 9-hole-only
- `--mode book` (default) books the earliest in-window slot, verifies it
  via reservation history, notifies, and stops; `--mode notify` pushes
  ntfy/email and keeps watching (re-alerts only on newly appeared times)
- `--prefer "6:00 PM"` switches book mode from greedy (first course with
  an in-window slot wins) to two-phase: scan ALL courses, then attempt
  slots closest to the target first (ties go to the earlier time).
  `--success-note "..."` appends a reminder to the terminal notifications
  (booked / dry-run / needs-manual-check) — e.g. "cancel the held slot"
- `--account-id <id>` books under a specific accounts.json account
  (WebTrac holds one tee time per account per day, so an upgrade watch
  should book under a different account than the slot you're holding)
- Treats `unverified_post_click` as terminal NEEDS MANUAL CHECK — never
  tries another slot after an ambiguous checkout
- Fresh browser per cycle; per-cycle phantom blacklist so a "taken" slot
  isn't retried within the same cycle (but is re-checked next cycle)
- Ends when past `--end` (or when `--date` is in the past), with a final
  "watch ended" push so you know it's over
- Grep-able log markers: `BOOKED` / `FOUND` / `DRY-RUN OK` /
  `NEEDS MANUAL CHECK` / `LOGIN FAILED` / `CYCLE ERROR` / `WATCH ENDED` /
  `FATAL`

## Cancellation

Cancel a booked tee time from the dashboard (or CLI). **A confirmation
number is required** — WebTrac's `teetimecancel.html` is a search-by-
confirmation-number form, not a clickable list of reservations, and the
number is NOT shown in the reservation history. Get it from the WebTrac
booking email, or from **My Account → Reprint A Receipt**: the receipt PDF
lists the per-player confirmation numbers (comma-separated, e.g.
`324180014,324180016,...` for a 4-player slot — the form accepts the full
comma-separated list; proven live 2026-07-04). Note the receipt number
itself is NOT a confirmation number; searching with it finds nothing.
`fetch_receipt.py` automates the recovery: it logs in, downloads the
receipt PDF from the reprint page, and prints the confirmation numbers
(no PDF library needed — it zlib-inflates the content streams directly):

```bash
python3 fetch_receipt.py list --account michael          # recent receipts
python3 fetch_receipt.py get --account michael --receipt 7419958
```

The cancel-search result messages distinguish failure modes:
- "No tee times available for Confirmation Number and Time selected." —
  no match: wrong number, or wrong hour/minute/AM-PM
- "... and Time selected **that can be cancelled**." — the reservation
  matched but can't be cancelled (e.g. the tee time is already in the
  past). Past tee times simply age out; a $0 receipt means nothing owed.
The bot logs into the holding account, opens the
bare `teetimecancel.html` page (which mints a session-specific `_csrf_token`),
fills in the confirmation number + tee-time selects, submits the search,
confirms the cancel, and verifies the slot is no longer active in the
reservation history.

### CLI

```bash
# Run a queued cancel job (normally spawned by the dashboard Cancel button)
python3 cancel_bot.py run --job <id>
python3 cancel_bot.py run --job <id> --dry-run --headful

# One-off manual cancel (not persisted) — --confirmation is required
python3 cancel_bot.py cancel --account michael --date 6/13/2026 \
    --time "8:01 AM" --course "Roy Kizer" --confirmation R1234567
python3 cancel_bot.py cancel --account michael --date 6/13/2026 \
    --time "8:01 AM" --course "Roy Kizer" --confirmation R1234567 \
    --dry-run --headful
```

### Dashboard

Each future booking in the History section shows a **Cancel** button (one
per sub-booking for multi-account weekends). Clicking it pops a `prompt()`
dialog that names the exact slot, warns the cancel is permanent, and asks
for the confirmation number from the WebTrac email. Dismissing it or leaving
it blank aborts. Otherwise it POSTs to `/api/booking/cancel`, which spawns
the runner and polls `/api/cancel/jobs` until the job reaches `done`/`failed`.
Cancelled bookings render as "Cancelled".

### How it works

- `cancel_queue.py` manages the cancel queue (`cancel_queue.json`) with the
  same `fcntl.flock` atomic read-modify-write as `standby_queue.py`. The
  confirmation number is a required, frozen field on each job.
- `cancel_bot.py run --job <id>` loads the job, logs in as the holding
  account, drives the `teetimecancel.html` form (confirmation number + the
  three hour/minute/AM-PM selects), submits the search, confirms the cancel,
  verifies the slot is no longer active, marks it `cancelled` in
  `history.json`, and notifies.
- A dup-spawn guard (`find_active_for`) means a double-click can't fire two
  cancels for the same slot — the second POST returns the existing job.
- Result codes: `cancelled` (verified no longer active), `dry_run`, or
  `failed: <reason>` (no matching reservation, confirm step not actionable,
  history unreachable, etc.).
- **Verify-gone is status-aware.** A cancelled reservation is NOT removed
  from the WebTrac history table — its Status cell merely flips from
  "Reserved" to "Cancelled". So `bot._active_slot_in_content` requires a
  slot-matching row whose Status still reads "Reserved"; a flat text match
  would forever report "still present".

### Safety (cancellation is irreversible)

- `--dry-run` navigates to the cancel page, fills + submits the search, dumps
  the result page, and **stops before confirming the cancel**
- Strict hour/minute/AM-PM parse of the stored time into the form's selects;
  an unparseable time or a missing confirmation number aborts before the form
  is touched
- Post-cancel verify-gone: success is only reported if the slot is no longer
  active in history afterward (an unreachable history page is `failed`, never
  a false success)
- The dashboard always gates a real cancel behind the `prompt()` dialog, and
  the runner always dumps the cancel-page HTML to `debug_screenshots/`

The full flow was mapped live on 2026-07-03 (real cancel of a 7/04 Morris
Williams booking): after Search matches, a Continue anchor
(`#webteetimecancel_buttonaddtocart`) routes through
`addtocart.html?action=cancellation` — the same cart checkout as booking —
then `#webcart_buttoncheckout` (Proceed To Checkout) and
`#webcheckout_buttoncontinue` must be clicked before `confirmation.html`
is reached. `_confirm_cancel` walks these steps and returns success ONLY
if a confirmation/receipt URL is reached: cancellation items stranded in
the cart also hide the "Reserved" history row, so verify-gone alone reads
a pending-in-cart cancel as done (this false success happened live before
the checkout walk was added). The live not-found message is "No tee times
available for Confirmation Number and Time selected."

## Tests

```bash
python3 -m pytest tests/ -q
```

`tests/test_pure.py` covers pure functions (parse_time, is_time_in_range
for both morning and fallback windows, get_time_priority ordering,
get_next_weekend_dates, phantom blacklist tuple shape, course config
integrity, and player-count fallback config). 30 tests.

`tests/test_standby.py` covers standby queue operations (add, cancel,
expire, mark booked, active filtering, cleanup, `min_players` and
`max_hour` validation). 40 tests.

`tests/test_standby_bot.py` covers `_player_counts` (the min_players floor
walks 4→3 and never below; legacy watches without the key still fall back
to FALLBACK_NUM_PLAYERS) and `_search_window` (max_hour caps the pref
window; wider caps are ignored; legacy watches unaffected).

`tests/test_cancel_queue.py` covers the cancel queue (add/get/list/update,
validation including the required `confirmation_number`, `find_active_for`
dup guard, `clear_old_jobs`).

`tests/test_today_watch.py` covers the same-day watcher's pure helpers:
`parse_window` (boundaries, noon/midnight, unparseable/inverted),
`slots_in_window` (inclusive bounds, AM/PM, unparseable times),
`date_relation` (padding-agnostic past/today/future, malformed raises),
`parse_holes`, and the `build_search_url` holes parameter.

`tests/test_cancel.py` covers the bot.py cancel refactor: `_slot_in_content`
(date padding + condensed "8:01A" time + course), the status-aware
`_active_slot_in_content` (cancelled rows persist in history → must read as
not-active), `_fetch_history_content` (timeout/login/queue → None), and
`mark_booking_cancelled` (multi + single + legacy-no-course shapes,
idempotent, wrong-account/time/date no-ops).

`tests/test_cancel_bot.py` covers the form-driving helpers: `_parse_time_to_slots`
(zero-padding + AM/PM + range/format validation), `_slot_for`, `_set_combobox`
(native select_option, the JS fallback for the Vue-hidden selects, and
read-back verification), the `_fill_cancel_form` orchestration (fills
confirmation number + the three selects + Search; bails on missing
confirmation number, unparseable time, or any select that won't hold its
value — submitting with a wrong AM/PM searches the wrong reservation),
and the `_search_found_reservation` not-found heuristic including the
live "No tee times available" message.

`tests/test_fetch_receipt.py` covers `extract_confirmation_numbers` (the
zlib-stream PDF parse: compressed/uncompressed/CRLF streams, the
Confirmation-label guard for lone numbers, digit-boundary anchoring so
phone numbers and household ids never match, multi-tee-time receipts,
dedupe).

`tests/test_monitor_cancel.py` spins up the real monitor `Handler` on an
ephemeral port (browser spawn + account lookup stubbed) to exercise the
`/api/booking/cancel` and `/api/cancel/jobs` routes, the dup-spawn guard, and
the required-confirmation-number validation.

There are no browser-integration tests — the only way to verify the
Playwright booking/cancel path is `--dry-run` against the live site.

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
