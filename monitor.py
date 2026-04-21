#!/usr/bin/env python3
"""Live monitoring dashboard for the golf booking bot.

Run:  python3 monitor.py
Then open: http://localhost:8111
"""

import http.server
import json
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Golf Bot</title>
<style>
  :root {
    --bg: #09090b;
    --surface: #18181b;
    --surface-2: #27272a;
    --border: #27272a;
    --border-strong: #3f3f46;
    --text: #fafafa;
    --text-muted: #a1a1aa;
    --text-subtle: #71717a;
    --success: #22c55e;
    --success-bg: rgba(34, 197, 94, 0.1);
    --danger: #ef4444;
    --danger-bg: rgba(239, 68, 68, 0.1);
    --warning: #eab308;
    --warning-bg: rgba(234, 179, 8, 0.1);
    --accent: #3b82f6;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    min-height: 100vh;
  }

  .container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 32px 24px;
  }

  /* Header */
  .header {
    display: flex; align-items: center; gap: 16px;
    margin-bottom: 32px; padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .header h1 {
    font-size: 20px; font-weight: 600; letter-spacing: -0.02em;
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--text-subtle);
    flex-shrink: 0;
  }
  .status-dot.active {
    background: var(--success); animation: pulse 2s infinite;
  }
  .status-dot.success { background: var(--success); }
  .status-dot.failed { background: var(--danger); }
  @keyframes pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4); }
    50% { box-shadow: 0 0 0 6px rgba(34, 197, 94, 0); }
  }
  .status-text {
    font-size: 13px; color: var(--text-muted);
  }
  .next-run {
    margin-left: auto; font-size: 13px; color: var(--text-muted);
    display: flex; align-items: center; gap: 8px;
  }
  .next-run-value { color: var(--text); font-weight: 500; }

  /* Stat grid */
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 24px;
  }
  .stat {
    background: var(--bg);
    padding: 16px 20px;
    display: flex; flex-direction: column; gap: 4px;
  }
  .stat-label {
    font-size: 11px; font-weight: 500; color: var(--text-subtle);
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  .stat-value {
    font-size: 15px; font-weight: 500; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .stat-value.success { color: var(--success); }
  .stat-value.danger { color: var(--danger); }
  .stat-value.warning { color: var(--warning); }
  .stat-value.muted { color: var(--text-muted); font-weight: 400; }

  /* Main grid */
  .main-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }
  @media (max-width: 900px) { .main-grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    display: flex; flex-direction: column;
  }
  .card-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    font-size: 13px;
  }
  .card-title { font-weight: 500; color: var(--text); }
  .card-meta { color: var(--text-subtle); font-size: 12px; }

  /* Browser view */
  .browser-body { padding: 12px; }
  .browser-body img {
    width: 100%; display: block; border-radius: 4px;
    border: 1px solid var(--border);
  }
  .browser-empty {
    aspect-ratio: 16 / 10;
    display: flex; align-items: center; justify-content: center;
    color: var(--text-subtle); text-align: center; font-size: 13px;
    border: 1px dashed var(--border);
    border-radius: 4px;
  }

  /* Log */
  .log-body {
    padding: 12px 16px;
    overflow-y: auto;
    max-height: 520px;
    font-family: ui-monospace, "SF Mono", "Menlo", "Monaco", monospace;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap; word-break: break-word;
  }
  .log-body::-webkit-scrollbar { width: 8px; }
  .log-body::-webkit-scrollbar-track { background: var(--bg); }
  .log-body::-webkit-scrollbar-thumb { background: var(--surface-2); border-radius: 4px; }
  .line { padding: 0 4px; color: var(--text-muted); }
  .line.log-success { color: var(--success); }
  .line.log-error { color: var(--danger); }
  .line.log-warn { color: var(--warning); }
  .line.log-header { color: var(--text); font-weight: 500; }
  .line.log-dim { color: var(--text-subtle); }
  .empty-state {
    padding: 48px 24px; color: var(--text-subtle);
    text-align: center; font-size: 13px;
  }

  /* History */
  .history-table {
    width: 100%; border-collapse: collapse; font-size: 13px;
  }
  .history-table th {
    text-align: left;
    padding: 10px 16px;
    color: var(--text-subtle);
    font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  .history-table td {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    color: var(--text);
  }
  .history-table tr:last-child td { border-bottom: none; }
  .history-table tr:hover td { background: rgba(255, 255, 255, 0.02); }
  .history-table td.muted { color: var(--text-muted); }

  /* Badges */
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 12px; font-weight: 500;
    white-space: nowrap;
  }
  .badge.success { background: var(--success-bg); color: var(--success); }
  .badge.danger { background: var(--danger-bg); color: var(--danger); }
  .badge.warning { background: var(--warning-bg); color: var(--warning); }
  .badge.neutral { background: var(--surface); color: var(--text-muted); }
  .badge-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: currentColor;
  }

  .overall-badge {
    padding: 3px 8px; font-size: 11px;
  }
</style>
</head>
<body>

<div class="container">

  <header class="header">
    <div class="status-dot" id="statusDot"></div>
    <h1>Golf Bot</h1>
    <span class="status-text" id="statusText">Loading...</span>
    <div class="next-run">
      <span>Next run</span>
      <span class="next-run-value" id="nextRun">—</span>
    </div>
  </header>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Phase</div>
      <div class="stat-value muted" id="phase">Idle</div>
    </div>
    <div class="stat">
      <div class="stat-label">Saturday</div>
      <div class="stat-value muted" id="satResult">—</div>
    </div>
    <div class="stat">
      <div class="stat-label">Sunday</div>
      <div class="stat-value muted" id="sunResult">—</div>
    </div>
    <div class="stat">
      <div class="stat-label">Players</div>
      <div class="stat-value muted" id="players">—</div>
    </div>
    <div class="stat">
      <div class="stat-label">Release countdown</div>
      <div class="stat-value muted" id="countdown">—</div>
    </div>
    <div class="stat">
      <div class="stat-label">Last update</div>
      <div class="stat-value muted" id="lastUpdate">—</div>
    </div>
  </div>

  <div class="main-grid">

    <div class="card">
      <div class="card-header">
        <span class="card-title">Browser view</span>
        <span class="card-meta" id="browserLabel">no screenshot</span>
      </div>
      <div class="browser-body">
        <img id="browserImg" src="/api/screenshot" style="display:none" alt="live screenshot">
        <div class="browser-empty" id="browserEmpty">
          Bot isn't running. Live screenshot will appear here during a run.
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span class="card-title">Run log</span>
        <span class="card-meta" id="lineCount">0 lines</span>
      </div>
      <div class="log-body" id="logContent">
        <div class="empty-state">Log is empty. Waiting for the next run.</div>
      </div>
    </div>

  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title">Run history</span>
      <span class="card-meta" id="historyCount">0 runs</span>
    </div>
    <div id="historyBody">
      <div class="empty-state">No past runs recorded yet.</div>
    </div>
  </div>

</div>

<script>
let prevLength = 0;
let nextRunSeconds = 0;

function formatDuration(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
}

function classifyLine(text) {
  const t = text.toLowerCase();
  if (t.includes('booked!') || t.includes('verified') || t.includes('dry-run ok') || t.includes('success —')) return 'log-success';
  if (t.includes('failed') || t.includes('error') || t.includes('timeout') || t.includes('crash')) return 'log-error';
  if (t.includes('warn') || t.includes('retry') || t.includes('taken') || t.includes('session expired')) return 'log-warn';
  if (t.includes('===') || t.includes('***') || t.startsWith('session')) return 'log-header';
  if (t.includes('until release') || t.includes('sleeping') || t.includes('waiting')) return 'log-dim';
  return '';
}

function parseResults(lines) {
  let sat = '—', sun = '—';
  let satClass = 'muted', sunClass = 'muted';
  let isRunning = false;
  let phase = 'Idle';
  let phaseClass = 'muted';
  let countdown = '—';
  let players = '—';

  for (const line of lines) {
    const t = line.toLowerCase();

    const sessionMatch = line.match(/SESSION (\\d+)/);
    if (sessionMatch) { isRunning = true; }

    const playerMatch = line.match(/Players:\\s*(\\d+)/);
    if (playerMatch) { players = playerMatch[1]; }
    if (t.includes('retrying with') && t.includes('players')) {
      const fbMatch = line.match(/retrying with (\\d+) players/i);
      if (fbMatch) players = fbMatch[1] + ' (fallback)';
    }

    if (t.includes('austin golf tee time')) { phase = 'Starting'; phaseClass = ''; }
    if (t.includes('logging in')) { phase = 'Logging in'; phaseClass = 'warning'; }
    if (t.includes('[login] success')) { phase = 'Logged in'; phaseClass = 'success'; }
    if (t.includes('login failed') || t.includes('[login] failed')) { phase = 'Login failed'; phaseClass = 'danger'; }
    if (t.includes('[queue]') && (t.includes('waiting') || t.includes('still waiting'))) { phase = 'Waiting in queue'; phaseClass = 'warning'; }
    if (t.includes('[queue]') && t.includes('released')) { phase = 'Through queue'; phaseClass = 'success'; }
    if (t.includes('waiting until') && t.includes('release')) { phase = 'Waiting for release'; phaseClass = 'warning'; }
    if (t.includes('release time reached')) { phase = 'Searching'; phaseClass = ''; }
    if (t.includes('already past release')) { phase = 'Searching'; phaseClass = ''; }
    if (t.includes('booking saturday')) { phase = 'Searching Saturday'; phaseClass = ''; }
    if (t.includes('booking sunday')) { phase = 'Searching Sunday'; phaseClass = ''; }
    if (t.includes('morning pass')) { phase = phase.split(' —')[0] + ' (morning)'; }
    if (t.includes('fallback pass')) { phase = phase.split(' —')[0] + ' (afternoon)'; }
    if (t.includes('[recovery]')) { phase = 'Recovering'; phaseClass = 'danger'; }
    if (t.includes('verified')) { phase = 'Booking verified'; phaseClass = 'success'; }
    if (t.includes('final results')) { phase = 'Done'; phaseClass = 'success'; }
    if (t.includes('max time') && t.includes('exceeded')) { phase = 'Timed out'; phaseClass = 'danger'; }

    const countdownMatch = line.match(/(\\d+)s until release/);
    if (countdownMatch) {
      const secs = parseInt(countdownMatch[1]);
      countdown = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
    }
    if (t.includes('release time reached') || t.includes('already past release')) { countdown = 'Live'; }

    if (t.includes('saturday') && t.includes('success')) {
      const detail = line.match(/SUCCESS.*?[—-]\\s*(.+)/);
      sat = detail ? detail[1].trim() : 'Booked'; satClass = 'success';
    }
    if (t.includes('sunday') && t.includes('success')) {
      const detail = line.match(/SUCCESS.*?[—-]\\s*(.+)/);
      sun = detail ? detail[1].trim() : 'Booked'; sunClass = 'success';
    }
    if (t.includes('saturday') && t.includes('no booking')) { sat = 'No booking'; satClass = 'danger'; }
    if (t.includes('sunday') && t.includes('no booking')) { sun = 'No booking'; sunClass = 'danger'; }

    if (t.includes('booking saturday') && sat === '—') { sat = 'Searching…'; satClass = 'warning'; }
    if (t.includes('booking sunday') && sun === '—') { sun = 'Searching…'; sunClass = 'warning'; }
  }

  return { sat, sun, satClass, sunClass, isRunning, phase, phaseClass, countdown, players };
}

async function refreshLog() {
  try {
    const resp = await fetch('/api/log');
    const data = await resp.json();
    const lines = data.lines;

    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});

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
      setStat('satResult', r.sat, r.satClass);
      setStat('sunResult', r.sun, r.sunClass);
      setStat('phase', r.phase, r.phaseClass);
      setStat('players', r.players, r.players === '—' ? 'muted' : '');
      setStat('countdown', r.countdown, r.countdown === '—' ? 'muted' : '');

      const dot = document.getElementById('statusDot');
      const statusText = document.getElementById('statusText');
      if (r.phase === 'Done') {
        const allBooked = r.satClass === 'success' && r.sunClass === 'success';
        dot.className = 'status-dot ' + (allBooked ? 'success' : 'failed');
        statusText.textContent = allBooked ? 'Both days booked' : 'Finished';
      } else if (r.phase === 'Timed out') {
        dot.className = 'status-dot failed';
        statusText.textContent = 'Timed out';
      } else if (r.phase === 'Waiting in queue') {
        dot.className = 'status-dot active';
        statusText.textContent = 'In waiting room';
      } else if (r.phase.includes('Waiting')) {
        dot.className = 'status-dot active';
        statusText.textContent = 'Waiting';
      } else if (r.isRunning) {
        dot.className = 'status-dot active';
        statusText.textContent = 'Running';
      }
    }
  } catch (e) {}
}

function setStat(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'stat-value ' + (cls || '');
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
      label.textContent = info.label || new Date(info.mtime * 1000).toLocaleTimeString();
    } else {
      img.style.display = 'none';
      empty.style.display = 'flex';
      label.textContent = 'no screenshot';
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

function tickNextRun() {
  if (nextRunSeconds > 0) nextRunSeconds -= 1;
  const el = document.getElementById('nextRun');
  el.textContent = nextRunSeconds <= 0 ? 'now' : formatDuration(nextRunSeconds);
}

function formatTimestamp(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const dateStr = d.toLocaleDateString([], {month: 'short', day: 'numeric'});
    const timeStr = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    return dateStr + ', ' + timeStr;
  } catch (e) { return iso; }
}

function runDuration(started, ended) {
  if (!started || !ended) return '—';
  try {
    const sec = Math.max(0, Math.round((new Date(ended) - new Date(started)) / 1000));
    return formatDuration(sec);
  } catch (e) { return '—'; }
}

function renderHistory(entries) {
  const body = document.getElementById('historyBody');
  const count = document.getElementById('historyCount');
  count.textContent = entries.length + (entries.length === 1 ? ' run' : ' runs');

  if (!entries.length) {
    body.innerHTML = '<div class="empty-state">No past runs recorded yet.</div>';
    return;
  }

  let html = '<table class="history-table"><thead><tr>';
  html += '<th style="width:140px">Run date</th>';
  html += '<th style="width:110px">Status</th>';
  html += '<th>Saturday</th>';
  html += '<th>Sunday</th>';
  html += '<th style="width:100px">Duration</th>';
  html += '</tr></thead><tbody>';

  for (const e of entries) {
    const sat = e.results?.saturday;
    const sun = e.results?.sunday;
    const satOk = !!sat?.success;
    const sunOk = !!sun?.success;
    const satText = satOk ? sat.details : 'No booking';
    const sunText = sunOk ? sun.details : 'No booking';

    let overall, overallClass;
    if (satOk && sunOk) { overall = 'Success'; overallClass = 'success'; }
    else if (satOk || sunOk) { overall = 'Partial'; overallClass = 'warning'; }
    else { overall = 'Failed'; overallClass = 'danger'; }

    html += '<tr>';
    html += '<td class="muted">' + escapeHtml(formatTimestamp(e.run_started)) + '</td>';
    html += '<td><span class="badge ' + overallClass + '"><span class="badge-dot"></span>' + overall + '</span></td>';
    html += '<td>' + renderBookingCell(satText, satOk) + '</td>';
    html += '<td>' + renderBookingCell(sunText, sunOk) + '</td>';
    html += '<td class="muted">' + escapeHtml(runDuration(e.run_started, e.run_ended)) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';
  body.innerHTML = html;
}

function renderBookingCell(text, ok) {
  const cls = ok ? 'success' : 'danger';
  const icon = ok ? '✓' : '✗';
  return '<span style="color: var(--' + cls + ')">' + icon + '</span> ' + escapeHtml(text);
}

async function refreshHistory() {
  try {
    const resp = await fetch('/api/history');
    const entries = await resp.json();
    renderHistory(entries);
    if (entries[0] && prevLength === 0) applyHistoryToStatus(entries[0]);
  } catch (e) {}
}

function applyHistoryToStatus(entry) {
  const sat = entry.results?.saturday;
  const sun = entry.results?.sunday;
  setStat('satResult', sat?.success ? sat.details : 'No booking', sat?.success ? 'success' : 'danger');
  setStat('sunResult', sun?.success ? sun.details : 'No booking', sun?.success ? 'success' : 'danger');
  setStat('phase', 'Idle — last ran ' + formatTimestamp(entry.run_started), '');

  const dot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const allBooked = sat?.success && sun?.success;
  dot.className = 'status-dot ' + (allBooked ? 'success' : 'failed');
  statusText.textContent = allBooked ? 'Last run: both booked' : 'Last run: incomplete';
}

function escapeHtml(t) {
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g, '&quot;');
}

setInterval(refreshLog, 1000);
setInterval(refreshScreenshot, 2000);
setInterval(tickNextRun, 1000);
setInterval(refreshNextRun, 60000);
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
        pass


if __name__ == "__main__":
    print(f"Golf Bot Monitor running at http://localhost:{PORT}")
    print("Open this URL in your browser to watch the bot.")
    print("Press Ctrl+C to stop.\n")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
