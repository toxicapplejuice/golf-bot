#!/usr/bin/env python3
"""Live monitoring dashboard for the golf booking bot.

Run:  python3 monitor.py
Then open: http://localhost:8111
"""

import http.server
import json
import os

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "booking.log")
PORT = 8111

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
  .status-dot.active {
    background: #3fb950; animation: pulse 2s infinite;
  }
  .status-dot.success {
    background: #3fb950; animation: none;
  }
  .status-dot.failed {
    background: #f85149; animation: none;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.4); }
    50% { opacity: 0.8; box-shadow: 0 0 0 8px rgba(63, 185, 80, 0); }
  }
  .status-text { font-size: 14px; color: #8b949e; }
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
  .log-container {
    background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
    overflow: hidden;
  }
  .log-header {
    padding: 10px 16px; background: #161b22; border-bottom: 1px solid #30363d;
    font-size: 13px; color: #8b949e; display: flex; justify-content: space-between;
  }
  .log-content {
    padding: 16px; overflow-y: auto; max-height: calc(100vh - 280px);
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
</style>
</head>
<body>

<div class="header">
  <div class="status-dot" id="statusDot"></div>
  <h1>Golf Bot Monitor</h1>
  <span class="status-text" id="statusText">Waiting for data...</span>
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
    <div class="card-label">Countdown</div>
    <div class="card-value" id="countdown" style="font-size:16px">--</div>
  </div>
  <div class="card">
    <div class="card-label">Last Update</div>
    <div class="card-value" id="lastUpdate" style="font-size:16px">--</div>
  </div>
</div>

<div class="log-container">
  <div class="log-header">
    <span>Live Log</span>
    <span id="lineCount">0 lines</span>
  </div>
  <div class="log-content" id="logContent">
    <div class="empty">Waiting for bot to start... Log will appear here automatically.</div>
  </div>
</div>

<script>
let prevLength = 0;

function classifyLine(text) {
  const t = text.toLowerCase();
  if (t.includes('booked!') || t.includes('success') || t.includes('dry-run ok')) return 'highlight-book';
  if (t.includes('failed') || t.includes('error') || t.includes('timeout')) return 'highlight-error';
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

    // Session tracking
    const sessionMatch = line.match(/SESSION (\\d+)/);
    if (sessionMatch) { session = '#' + sessionMatch[1]; isRunning = true; }

    // Players
    const playerMatch = line.match(/Players:\\s*(\\d+)/);
    if (playerMatch) { players = playerMatch[1]; }

    // Fallback player detection
    if (t.includes('retrying with') && t.includes('players')) {
      const fbMatch = line.match(/retrying with (\\d+) players/i);
      if (fbMatch) players = fbMatch[1] + ' (fallback)';
    }

    // Phase detection — later lines overwrite earlier ones
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
    if (t.includes('final results')) { phase = 'Done'; phaseClass = 'booked'; }
    if (t.includes('max time') && t.includes('exceeded')) { phase = 'Timed out'; phaseClass = 'failed'; }

    // Countdown to release
    const countdownMatch = line.match(/(\\d+)s until release/);
    if (countdownMatch) {
      const secs = parseInt(countdownMatch[1]);
      const m = Math.floor(secs / 60);
      const s = secs % 60;
      countdown = m + ':' + String(s).padStart(2, '0');
    }
    if (t.includes('release time reached') || t.includes('already past release')) { countdown = 'GO!'; }

    // Saturday/Sunday results
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

    // In-progress detection
    if (t.includes('booking saturday') && sat === '--') { sat = 'Searching...'; satClass = 'pending'; }
    if (t.includes('booking sunday') && sun === '--') { sun = 'Searching...'; sunClass = 'pending'; }

    // Booked mid-log (before final results)
    if (t.includes('booked!')) {
      const bookDetail = line.match(/\\[book\\]\\s*(.+?)\\.\\.\\./i);
      if (bookDetail) {
        // Figure out which day based on current phase
        if (phase.toLowerCase().includes('sunday')) {
          sun = bookDetail[1].trim() + ' ✓'; sunClass = 'booked';
        } else {
          sat = bookDetail[1].trim() + ' ✓'; satClass = 'booked';
        }
      }
    }
  }

  return { sat, sun, satClass, sunClass, session, isRunning, phase, phaseClass, countdown, players };
}

async function refresh() {
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

function escapeHtml(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/log":
            lines = []
            try:
                with open(LOG_FILE, "r") as f:
                    lines = [l.rstrip("\n") for l in f.readlines()]
            except FileNotFoundError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps({"lines": lines}).encode())
        elif self.path == "/" or self.path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        else:
            self.send_response(404)
            self.end_headers()

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
