#!/usr/bin/env python3
"""Live monitoring dashboard for the golf booking bot.

Run:  python3 monitor.py
Then open: http://localhost:8111
"""

import http.server
import json
import mimetypes
import os
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "booking.log")
LIVE_SCREENSHOT = os.path.join(SCRIPT_DIR, "debug_screenshots", "live.png")
LIVE_LABEL = os.path.join(SCRIPT_DIR, "debug_screenshots", "live_label.txt")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "history.json")
PORT = 8111


def seconds_until_next_monday_745pm() -> int:
    """Return seconds until the next Monday 7:45 PM local time."""
    now = datetime.now()
    target_weekday = 0  # Monday
    target_hour, target_minute = 19, 45
    # Days until next Monday
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return int((candidate - now).total_seconds())


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Golf Bot Monitor</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117; color: #c9d1d9; font-family: 'SF Mono', 'Consolas', monospace;
    padding: 20px; min-height: 100vh;
  }
  .header {
    display: flex; align-items: center; gap: 16px;
    padding: 16px 20px; margin-bottom: 20px;
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
  }
  .header h1 { font-size: 20px; color: #f0f6fc; font-weight: 600; }
  .status-dot {
    width: 12px; height: 12px; border-radius: 50%;
    background: #484f58; animation: none;
  }
  .status-dot.active { background: #3fb950; animation: pulse 2s infinite; }
  .status-dot.success { background: #3fb950; }
  .status-dot.failed { background: #f85149; }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.4); }
    50% { opacity: 0.8; box-shadow: 0 0 0 8px rgba(63, 185, 80, 0); }
  }
  .status-text { font-size: 14px; color: #8b949e; }
  .next-run-badge {
    margin-left: auto; font-size: 12px; color: #8b949e;
    background: #0d1117; padding: 6px 12px; border-radius: 6px;
    border: 1px solid #30363d;
  }
  .cards { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 14px 18px; flex: 1; min-width: 180px;
  }
  .card-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
  .card-value { font-size: 22px; color: #f0f6fc; margin-top: 4px; font-weight: 600; }
  .card-value.booked { color: #3fb950; }
  .card-value.pending { color: #d29922; }
  .card-value.failed { color: #f85149; }
  .main-layout {
    display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
  }
  @media (max-width: 1100px) { .main-layout { grid-template-columns: 1fr; } }
  .panel {
    background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
    overflow: hidden; display: flex; flex-direction: column;
  }
  .panel-header {
    padding: 10px 16px; background: #161b22; border-bottom: 1px solid #30363d;
    font-size: 13px; color: #8b949e; display: flex; justify-content: space-between;
  }
  .browser-view {
    padding: 12px; display: flex; flex-direction: column; gap: 8px;
    max-height: calc(100vh - 320px);
  }
  .browser-view img {
    width: 100%; border-radius: 6px; border: 1px solid #30363d;
    background: #161b22; display: block;
  }
  .browser-view .label {
    font-size: 12px; color: #8b949e; padding: 4px 8px;
    background: #161b22; border-radius: 6px;
  }
  .browser-view .empty-img {
    aspect-ratio: 16 / 10; display: flex; align-items: center; justify-content: center;
    color: #484f58; font-style: italic; text-align: center; padding: 20px;
    border: 1px dashed #30363d; border-radius: 6px;
  }
  .log-content {
    padding: 16px; overflow-y: auto; max-height: calc(100vh - 320px);
    font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-word;
  }
  .line { padding: 1px 0; }
  .line:hover { background: #161b22; }
  .line.highlight-book { color: #3fb950; font-weight: 600; }
  .line.highlight-error { color: #f85149; }
  .line.highlight-search { color: #79c0ff; }
  .line.highlight-queue { color: #d2a8ff; }
  .line.highlight-login { color: #d29922; }
  .line.highlight-header { color: #f0f6fc; font-weight: 600; }
  .line.highlight-wait { color: #8b949e; }
  .empty { color: #484f58; font-style: italic; padding: 40px; text-align: center; }
  .history-panel {
    margin-top: 20px;
    background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
    overflow: hidden;
  }
  .history-table {
    width: 100%; border-collapse: collapse; font-size: 13px;
  }
  .history-table th {
    text-align: left; padding: 10px 16px; background: #161b22;
    color: #8b949e; font-weight: 500; font-size: 11px;
    text-transform: uppercase; letter-spacing: 1px;
    border-bottom: 1px solid #30363d;
  }
  .history-table td {
    padding: 12px 16px; border-bottom: 1px solid #21262d; color: #c9d1d9;
  }
  .history-table tr:last-child td { border-bottom: none; }
  .history-table tr:hover td { background: #161b22; }
  .history-booked { color: #3fb950; }
  .history-failed { color: #f85149; }
  .history-partial { color: #d29922; }
  .history-empty { padding: 30px; color: #484f58; font-style: italic; text-align: center; }
</style>
</head>
<body>

<div class="header">
  <div class="status-dot" id="statusDot"></div>
  <h1>Golf Bot Monitor</h1>
  <span class="status-text" id="statusText">Waiting for data...</span>
  <span class="next-run-badge" id="nextRun">Next run: --</span>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Current Phase</div>
    <div class="card-value" id="phase" style="font-size:16px">Waiting to start</div>
  </div>
  <div class="card">
    <div class="card-label">Saturday</div>
    <div class="card-value pending" id="satResult">--</div>
  </div>
  <div class="card">
    <div class="card-label">Sunday</div>
    <div class="card-value pending" id="sunResult">--</div>
  </div>
  <div class="card">
    <div class="card-label">Players</div>
    <div class="card-value" id="players">--</div>
  </div>
  <div class="card">
    <div class="card-label">Countdown to 8pm</div>
    <div class="card-value" id="countdown" style="font-size:16px">--</div>
  </div>
  <div class="card">
    <div class="card-label">Last Update</div>
    <div class="card-value" id="lastUpdate" style="font-size:16px">--</div>
  </div>
</div>

<div class="main-layout">

  <div class="panel">
    <div class="panel-header">
      <span>Browser View (what the bot sees)</span>
      <span id="browserLabel">no screenshot yet</span>
    </div>
    <div class="browser-view">
      <img id="browserImg" src="/api/screenshot" style="display:none" alt="live screenshot">
      <div class="empty-img" id="browserEmpty">
        Bot isn't running.<br>Screenshot will appear here when the bot takes one.
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <span>Live Log</span>
      <span id="lineCount">0 lines</span>
    </div>
    <div class="log-content" id="logContent">
      <div class="empty">Waiting for bot to start...</div>
    </div>
  </div>

</div>

<div class="history-panel">
  <div class="panel-header">
    <span>Run History</span>
    <span id="historyCount">0 runs</span>
  </div>
  <div id="historyBody">
    <div class="history-empty">No past runs recorded yet.</div>
  </div>
</div>

<script>
let prevLength = 0;
let prevScreenshotTs = 0;
let nextRunSeconds = 0;

function formatDuration(totalSeconds) {
  if (totalSeconds < 60) return totalSeconds + 's';
  if (totalSeconds < 3600) {
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return m + 'm ' + s + 's';
  }
  if (totalSeconds < 86400) {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    return h + 'h ' + m + 'm';
  }
  const d = Math.floor(totalSeconds / 86400);
  const h = Math.floor((totalSeconds % 86400) / 3600);
  return d + 'd ' + h + 'h';
}

function classifyLine(text) {
  const t = text.toLowerCase();
  if (t.includes('booked!') || t.includes('verified') || t.includes('dry-run ok')) return 'highlight-book';
  if (t.includes('failed') || t.includes('error') || t.includes('timeout') || t.includes('crash')) return 'highlight-error';
  if (t.includes('[search]')) return 'highlight-search';
  if (t.includes('[queue]')) return 'highlight-queue';
  if (t.includes('[login]') || t.includes('logging in') || t.includes('login attempt')) return 'highlight-login';
  if (t.includes('===') || t.includes('***') || t.startsWith('session')) return 'highlight-header';
  if (t.includes('until release') || t.includes('s until release') || t.includes('sleeping')) return 'highlight-wait';
  return '';
}

function parseResults(lines) {
  let sat = '--', sun = '--', session = '--';
  let satClass = 'pending', sunClass = 'pending';
  let isRunning = false;
  let phase = 'Waiting to start';
  let phaseClass = '';
  let countdown = '--';
  let players = '--';

  for (const line of lines) {
    const t = line.toLowerCase();

    const sessionMatch = line.match(/SESSION (\\d+)/);
    if (sessionMatch) { session = '#' + sessionMatch[1]; isRunning = true; }

    const playerMatch = line.match(/Players:\\s*(\\d+)/);
    if (playerMatch) { players = playerMatch[1]; }

    if (t.includes('retrying with') && t.includes('players')) {
      const fbMatch = line.match(/retrying with (\\d+) players/i);
      if (fbMatch) players = fbMatch[1] + ' (fallback)';
    }

    if (t.includes('austin golf tee time')) { phase = 'Starting up'; phaseClass = ''; }
    if (t.includes('logging in')) { phase = 'Logging in'; phaseClass = 'pending'; }
    if (t.includes('[login] success')) { phase = 'Logged in'; phaseClass = 'booked'; }
    if (t.includes('login failed') || t.includes('[login] failed')) { phase = 'Login failed'; phaseClass = 'failed'; }
    if (t.includes('[queue]') && t.includes('waiting')) { phase = 'In waiting room'; phaseClass = 'pending'; }
    if (t.includes('[queue]') && t.includes('still waiting')) { phase = 'In waiting room'; phaseClass = 'pending'; }
    if (t.includes('[queue]') && t.includes('released')) { phase = 'Through waiting room'; phaseClass = 'booked'; }
    if (t.includes('waiting until') && t.includes('release')) { phase = 'Waiting for 8:00 PM'; phaseClass = 'pending'; }
    if (t.includes('release time reached')) { phase = 'Searching for tee times'; phaseClass = 'booked'; }
    if (t.includes('already past release')) { phase = 'Searching for tee times'; phaseClass = 'booked'; }
    if (t.includes('booking saturday')) { phase = 'Searching Saturday'; phaseClass = 'pending'; }
    if (t.includes('booking sunday')) { phase = 'Searching Sunday'; phaseClass = 'pending'; }
    if (t.includes('morning pass')) { phase = phase.split(' —')[0] + ' — morning'; phaseClass = 'pending'; }
    if (t.includes('fallback pass')) { phase = phase.split(' —')[0] + ' — afternoon'; phaseClass = 'pending'; }
    if (t.includes('[recovery]')) { phase = 'Recovering...'; phaseClass = 'failed'; }
    if (t.includes('verified')) { phase = 'Verified booking'; phaseClass = 'booked'; }
    if (t.includes('final results')) { phase = 'Done'; phaseClass = 'booked'; }
    if (t.includes('max time') && t.includes('exceeded')) { phase = 'Timed out'; phaseClass = 'failed'; }

    const countdownMatch = line.match(/(\\d+)s until release/);
    if (countdownMatch) {
      const secs = parseInt(countdownMatch[1]);
      const m = Math.floor(secs / 60);
      const s = secs % 60;
      countdown = m + ':' + String(s).padStart(2, '0');
    }
    if (t.includes('release time reached') || t.includes('already past release')) { countdown = 'GO!'; }

    if (t.includes('saturday') && t.includes('success')) {
      const detail = line.match(/SUCCESS.*?[—-]\\s*(.+)/);
      sat = detail ? detail[1].trim() : 'Booked'; satClass = 'booked';
    }
    if (t.includes('sunday') && t.includes('success')) {
      const detail = line.match(/SUCCESS.*?[—-]\\s*(.+)/);
      sun = detail ? detail[1].trim() : 'Booked'; sunClass = 'booked';
    }
    if (t.includes('saturday') && t.includes('no booking')) { sat = 'No booking'; satClass = 'failed'; }
    if (t.includes('sunday') && t.includes('no booking')) { sun = 'No booking'; sunClass = 'failed'; }

    if (t.includes('booking saturday') && sat === '--') { sat = 'Searching...'; satClass = 'pending'; }
    if (t.includes('booking sunday') && sun === '--') { sun = 'Searching...'; sunClass = 'pending'; }

    if (t.includes('verified')) {
      const v = line.match(/VERIFIED/i);
      if (v && phase.toLowerCase().includes('sunday')) { sun = (sun.replace(' ✓','')) + ' ✓'; sunClass = 'booked'; }
      else if (v) { sat = (sat.replace(' ✓','')) + ' ✓'; satClass = 'booked'; }
    }
  }

  return { sat, sun, satClass, sunClass, session, isRunning, phase, phaseClass, countdown, players };
}

async function refreshLog() {
  try {
    const resp = await fetch('/api/log');
    const data = await resp.json();
    const lines = data.lines;

    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();

    if (lines.length === 0) return;

    if (lines.length !== prevLength) {
      prevLength = lines.length;

      const container = document.getElementById('logContent');
      container.innerHTML = lines.map(l =>
        `<div class="line ${classifyLine(l)}">${escapeHtml(l)}</div>`
      ).join('');
      container.scrollTop = container.scrollHeight;

      document.getElementById('lineCount').textContent = lines.length + ' lines';

      const r = parseResults(lines);
      const satEl = document.getElementById('satResult');
      const sunEl = document.getElementById('sunResult');
      satEl.textContent = r.sat; satEl.className = 'card-value ' + r.satClass;
      sunEl.textContent = r.sun; sunEl.className = 'card-value ' + r.sunClass;

      const phaseEl = document.getElementById('phase');
      phaseEl.textContent = r.phase;
      phaseEl.className = 'card-value ' + (r.phaseClass || '');

      document.getElementById('players').textContent = r.players;
      document.getElementById('countdown').textContent = r.countdown;

      const dot = document.getElementById('statusDot');
      const statusText = document.getElementById('statusText');

      if (r.phase === 'Done') {
        const allBooked = r.satClass === 'booked' && r.sunClass === 'booked';
        dot.className = 'status-dot ' + (allBooked ? 'success' : 'failed');
        statusText.textContent = allBooked ? 'Both days booked!' : 'Finished';
      } else if (r.phase === 'Timed out') {
        dot.className = 'status-dot failed';
        statusText.textContent = 'Timed out';
      } else if (r.phase === 'In waiting room') {
        dot.className = 'status-dot active';
        statusText.textContent = 'In Queue-it waiting room...';
      } else if (r.phase.includes('Waiting for 8')) {
        dot.className = 'status-dot active';
        statusText.textContent = 'Waiting for tee time release...';
      } else if (r.isRunning) {
        dot.className = 'status-dot active';
        statusText.textContent = 'Bot is running...';
      }
    }
  } catch (e) {}
}

async function refreshScreenshot() {
  try {
    const resp = await fetch('/api/screenshot_info');
    const info = await resp.json();
    const img = document.getElementById('browserImg');
    const empty = document.getElementById('browserEmpty');
    const label = document.getElementById('browserLabel');

    if (info.exists) {
      img.src = '/api/screenshot?t=' + info.mtime;
      img.style.display = 'block';
      empty.style.display = 'none';
      label.textContent = info.label || ('updated ' + new Date(info.mtime * 1000).toLocaleTimeString());
    } else {
      img.style.display = 'none';
      empty.style.display = 'flex';
      label.textContent = 'no screenshot yet';
    }
  } catch (e) {}
}

async function refreshNextRun() {
  try {
    const resp = await fetch('/api/next_run');
    const info = await resp.json();
    nextRunSeconds = info.seconds;
    tickNextRun();
  } catch (e) {}
}

function formatTimestamp(iso) {
  if (!iso) return '--';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  } catch (e) { return iso; }
}

function formatDurationFromRun(started, ended) {
  if (!started || !ended) return '--';
  try {
    const sec = Math.max(0, Math.round((new Date(ended) - new Date(started)) / 1000));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m + 'm ' + s + 's';
  } catch (e) { return '--'; }
}

function renderHistory(entries) {
  const body = document.getElementById('historyBody');
  const count = document.getElementById('historyCount');
  count.textContent = entries.length + ' run' + (entries.length === 1 ? '' : 's');

  if (!entries.length) {
    body.innerHTML = '<div class="history-empty">No past runs recorded yet.</div>';
    return;
  }

  let html = '<table class="history-table">';
  html += '<thead><tr>';
  html += '<th>When</th><th>Weekend</th><th>Saturday</th><th>Sunday</th><th>Duration</th>';
  html += '</tr></thead><tbody>';

  for (const e of entries) {
    const sat = e.results?.saturday;
    const sun = e.results?.sunday;
    const satText = sat?.success ? sat.details : 'No booking';
    const sunText = sun?.success ? sun.details : 'No booking';
    const satClass = sat?.success ? 'history-booked' : 'history-failed';
    const sunClass = sun?.success ? 'history-booked' : 'history-failed';

    html += '<tr>';
    html += '<td>' + escapeHtml(formatTimestamp(e.run_started)) + '</td>';
    html += '<td>' + escapeHtml(e.weekend || '--') + '</td>';
    html += '<td class="' + satClass + '">' + escapeHtml(satText) + '</td>';
    html += '<td class="' + sunClass + '">' + escapeHtml(sunText) + '</td>';
    html += '<td>' + escapeHtml(formatDurationFromRun(e.run_started, e.run_ended)) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';
  body.innerHTML = html;
}

let latestHistoryEntry = null;

async function refreshHistory() {
  try {
    const resp = await fetch('/api/history');
    const entries = await resp.json();
    latestHistoryEntry = entries[0] || null;
    renderHistory(entries);

    // If there's no active bot run, show the most recent booking from history
    // in the status cards so the dashboard isn't stuck on stale log data.
    if (latestHistoryEntry && prevLength === 0) {
      applyHistoryToStatusCards(latestHistoryEntry);
    }
  } catch (e) {}
}

function applyHistoryToStatusCards(entry) {
  const sat = entry.results?.saturday;
  const sun = entry.results?.sunday;
  const satEl = document.getElementById('satResult');
  const sunEl = document.getElementById('sunResult');
  if (sat?.success) {
    satEl.textContent = sat.details; satEl.className = 'card-value booked';
  } else {
    satEl.textContent = 'No booking'; satEl.className = 'card-value failed';
  }
  if (sun?.success) {
    sunEl.textContent = sun.details; sunEl.className = 'card-value booked';
  } else {
    sunEl.textContent = 'No booking'; sunEl.className = 'card-value failed';
  }
  const phaseEl = document.getElementById('phase');
  phaseEl.textContent = 'Idle — last run: ' + formatTimestamp(entry.run_started);
  phaseEl.className = 'card-value';

  const dot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const allBooked = sat?.success && sun?.success;
  dot.className = 'status-dot ' + (allBooked ? 'success' : 'failed');
  statusText.textContent = allBooked ? 'Last run: both days booked' : 'Last run: incomplete';
}

function tickNextRun() {
  if (nextRunSeconds > 0) nextRunSeconds -= 1;
  const el = document.getElementById('nextRun');
  if (nextRunSeconds <= 0) {
    el.textContent = 'Next run: now!';
  } else {
    el.textContent = 'Next run in ' + formatDuration(nextRunSeconds);
  }
}

function escapeHtml(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

setInterval(refreshLog, 1000);
setInterval(refreshScreenshot, 2000);
setInterval(tickNextRun, 1000);
setInterval(refreshNextRun, 60000);  // resync each minute
setInterval(refreshHistory, 10000);
refreshLog();
refreshScreenshot();
refreshNextRun();
refreshHistory();
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/log":
            lines = []
            try:
                with open(LOG_FILE, "r") as f:
                    lines = [l.rstrip("\n") for l in f.readlines()]
            except FileNotFoundError:
                pass
            self._json({"lines": lines})

        elif path == "/api/screenshot_info":
            info = {"exists": False, "mtime": 0, "label": ""}
            if os.path.exists(LIVE_SCREENSHOT):
                info["exists"] = True
                info["mtime"] = int(os.path.getmtime(LIVE_SCREENSHOT))
                try:
                    with open(LIVE_LABEL, "r") as f:
                        info["label"] = f.read().strip()
                except Exception:
                    pass
            self._json(info)

        elif path == "/api/screenshot":
            if not os.path.exists(LIVE_SCREENSHOT):
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(LIVE_SCREENSHOT, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(500)
                self.end_headers()

        elif path == "/api/next_run":
            self._json({"seconds": seconds_until_next_monday_745pm()})

        elif path == "/api/history":
            try:
                with open(HISTORY_FILE, "r") as f:
                    entries = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                entries = []
            self._json(entries)

        elif path in ("/", ""):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())

        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, format, *args):
        pass  # suppress request logs


if __name__ == "__main__":
    print(f"Golf Bot Monitor running at http://localhost:{PORT}")
    print("Open this URL in your browser to watch the bot in real time.")
    print("Press Ctrl+C to stop.\n")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
