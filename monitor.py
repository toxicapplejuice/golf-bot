#!/usr/bin/env python3
"""Live monitoring dashboard for the Austin municipal tee time bot.

Multi-account aware: reads accounts.json + per-account logs/screenshots
and renders one row per account plus a shared-state summary card.

Run:  python3 monitor.py
Then open: http://localhost:8111
"""

import http.server
import json
import os
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_screenshots")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "history.json")
ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.json")
SHARED_STATE_FILE = os.path.join(SCRIPT_DIR, "shared_state.json")
PORT = 8111


def seconds_until_next_monday_745pm() -> int:
    now = datetime.now()
    target_weekday = 0
    target_hour, target_minute = 19, 45
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return int((candidate - now).total_seconds())


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    accounts = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("disabled"):
            continue
        if entry.get("username") == "REPLACE_ME" or entry.get("password") == "REPLACE_ME":
            continue
        accounts.append({
            "id": entry["id"],
            "display_name": entry.get("display_name", entry["id"]),
        })
    return accounts


def per_account_paths(account_id: str):
    return {
        "log": os.path.join(SCRIPT_DIR, f"booking_{account_id}.log"),
        "screenshot": os.path.join(DEBUG_DIR, f"live_{account_id}.png"),
        "label": os.path.join(DEBUG_DIR, f"live_label_{account_id}.txt"),
    }


def read_log_tail(path: str, n_lines: int = 30) -> list:
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    return [l.rstrip("\n") for l in lines[-n_lines:]]


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Austin Municipal Tee Time Bot</title>
<style>
  :root {
    --bg: #09090b;
    --surface: #18181b;
    --surface-2: #27272a;
    --border: #27272a;
    --text: #fafafa;
    --text-muted: #a1a1aa;
    --text-subtle: #71717a;
    --success: #22c55e;
    --success-bg: rgba(34, 197, 94, 0.1);
    --danger: #ef4444;
    --danger-bg: rgba(239, 68, 68, 0.1);
    --warning: #eab308;
    --warning-bg: rgba(234, 179, 8, 0.1);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    min-height: 100vh;
  }
  .container { max-width: 1600px; margin: 0 auto; padding: 32px 24px; }

  .header {
    display: flex; align-items: center; gap: 16px;
    margin-bottom: 28px; padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .header h1 { font-size: 20px; font-weight: 600; letter-spacing: -0.02em; }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--text-subtle); flex-shrink: 0;
  }
  .status-dot.active { background: var(--success); animation: pulse 2s infinite; }
  .status-dot.success { background: var(--success); }
  .status-dot.failed { background: var(--danger); }
  @keyframes pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4); }
    50% { box-shadow: 0 0 0 6px rgba(34, 197, 94, 0); }
  }
  .status-text { font-size: 13px; color: var(--text-muted); }
  .next-run {
    margin-left: auto; font-size: 13px; color: var(--text-muted);
    display: flex; align-items: center; gap: 8px;
  }
  .next-run-value { color: var(--text); font-weight: 500; }

  /* Shared-state summary card */
  .shared-state {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 24px;
    display: grid; grid-template-columns: repeat(2, 1fr);
    overflow: hidden;
  }
  .shared-day {
    padding: 16px 20px; border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 4px;
  }
  .shared-day:last-child { border-right: none; }
  .shared-label {
    font-size: 11px; color: var(--text-subtle);
    text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500;
  }
  .shared-value { font-size: 16px; color: var(--text); font-weight: 500; }
  .shared-value.booked { color: var(--success); }
  .shared-value.pending { color: var(--text-muted); font-weight: 400; }
  .shared-detail { font-size: 12px; color: var(--text-subtle); margin-top: 2px; }

  /* Account rows */
  .accounts { display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }
  .account-card {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden;
  }
  .account-header {
    display: flex; align-items: center; gap: 12px; padding: 12px 16px;
    border-bottom: 1px solid var(--border);
  }
  .account-name { font-weight: 500; color: var(--text); }
  .account-meta { color: var(--text-subtle); font-size: 12px; margin-left: auto; }
  .account-body {
    display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
    background: var(--border);
  }
  @media (max-width: 1000px) { .account-body { grid-template-columns: 1fr; } }
  .account-col { background: var(--bg); padding: 12px 16px; min-width: 0; }
  .account-col-title {
    font-size: 11px; color: var(--text-subtle); text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 8px;
  }
  .account-screenshot img {
    width: 100%; display: block; border-radius: 4px; border: 1px solid var(--border);
  }
  .account-screenshot-empty {
    aspect-ratio: 16 / 10; display: flex; align-items: center; justify-content: center;
    color: var(--text-subtle); font-size: 12px; border: 1px dashed var(--border); border-radius: 4px;
  }
  .account-log {
    font-family: ui-monospace, "SF Mono", "Menlo", "Monaco", monospace;
    font-size: 11px; line-height: 1.55; color: var(--text-muted);
    white-space: pre-wrap; word-break: break-word;
    max-height: 240px; overflow-y: auto;
  }
  .account-log::-webkit-scrollbar { width: 6px; }
  .account-log::-webkit-scrollbar-thumb { background: var(--surface-2); border-radius: 3px; }
  .account-log .log-success { color: var(--success); }
  .account-log .log-error { color: var(--danger); }
  .account-log .log-warn { color: var(--warning); }
  .account-log .log-header { color: var(--text); font-weight: 500; }

  /* History */
  .history-panel {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
  }
  .history-header {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    font-size: 13px;
  }
  .history-title { font-weight: 500; }
  .history-meta { color: var(--text-subtle); font-size: 12px; }
  .history-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .history-table th {
    text-align: left; padding: 10px 16px;
    color: var(--text-subtle); font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border); background: var(--bg);
  }
  .history-table td {
    padding: 14px 16px; border-bottom: 1px solid var(--border); color: var(--text);
  }
  .history-table tr:last-child td { border-bottom: none; }
  .history-table tr:hover td { background: rgba(255, 255, 255, 0.02); }
  .history-table td.muted { color: var(--text-muted); }
  .empty-state { padding: 40px; color: var(--text-subtle); text-align: center; font-size: 13px; }

  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 4px;
    font-size: 12px; font-weight: 500; white-space: nowrap;
  }
  .badge.success { background: var(--success-bg); color: var(--success); }
  .badge.danger  { background: var(--danger-bg); color: var(--danger); }
  .badge.warning { background: var(--warning-bg); color: var(--warning); }
  .badge-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
</style>
</head>
<body>

<div class="container">

  <header class="header">
    <div class="status-dot" id="statusDot"></div>
    <h1>Austin Municipal Tee Time Bot</h1>
    <span class="status-text" id="statusText">Loading...</span>
    <div class="next-run">
      <span>Next run</span>
      <span class="next-run-value" id="nextRun">—</span>
    </div>
  </header>

  <!-- Shared state summary -->
  <div class="shared-state">
    <div class="shared-day">
      <span class="shared-label">Saturday</span>
      <span class="shared-value pending" id="sharedSat">—</span>
      <span class="shared-detail" id="sharedSatBy"></span>
    </div>
    <div class="shared-day">
      <span class="shared-label">Sunday</span>
      <span class="shared-value pending" id="sharedSun">—</span>
      <span class="shared-detail" id="sharedSunBy"></span>
    </div>
  </div>

  <!-- Per-account rows -->
  <div class="accounts" id="accounts">
    <div class="empty-state" id="accountsEmpty">No enabled accounts found in accounts.json.</div>
  </div>

  <!-- Run history -->
  <div class="history-panel">
    <div class="history-header">
      <span class="history-title">Run history</span>
      <span class="history-meta" id="historyCount">0 runs</span>
    </div>
    <div id="historyBody">
      <div class="empty-state">No past runs recorded yet.</div>
    </div>
  </div>

</div>

<script>
let accounts = [];
let nextRunSeconds = 0;

function formatDuration(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
}

function formatTimestamp(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString([], {month: 'short', day: 'numeric'})
           + ', ' + d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  } catch (e) { return iso; }
}

function classifyLine(text) {
  const t = text.toLowerCase();
  if (t.includes('booked!') || t.includes('verified') || t.includes('dry-run ok') || t.includes('success')) return 'log-success';
  if (t.includes('failed') || t.includes('error') || t.includes('timeout') || t.includes('crash')) return 'log-error';
  if (t.includes('warn') || t.includes('taken') || t.includes('session expired') || t.includes('retry')) return 'log-warn';
  if (t.includes('===') || t.includes('***') || t.startsWith('session')) return 'log-header';
  return '';
}

function escapeHtml(t) {
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g, '&quot;');
}

function phaseFromLog(lines) {
  // Walk from end — most recent signal wins
  let phase = 'Idle';
  let phaseClass = '';
  for (const line of lines) {
    const t = line.toLowerCase();
    if (t.includes('austin golf tee time')) { phase = 'Starting'; phaseClass = ''; }
    if (t.includes('logging in')) { phase = 'Logging in'; phaseClass = 'warning'; }
    if (t.includes('[login] success')) { phase = 'Logged in'; phaseClass = 'success'; }
    if (t.includes('login failed') || t.includes('[login] failed')) { phase = 'Login failed'; phaseClass = 'danger'; }
    if (t.includes('[queue]') && (t.includes('waiting') || t.includes('still waiting'))) { phase = 'Waiting in queue'; phaseClass = 'warning'; }
    if (t.includes('[queue]') && t.includes('released')) { phase = 'Through queue'; phaseClass = 'success'; }
    if (t.includes('waiting until') && t.includes('release')) { phase = 'Waiting for release'; phaseClass = 'warning'; }
    if (t.includes('release time reached')) { phase = 'Searching'; phaseClass = ''; }
    if (t.includes('booking saturday')) { phase = 'Searching Saturday'; phaseClass = ''; }
    if (t.includes('booking sunday')) { phase = 'Searching Sunday'; phaseClass = ''; }
    if (t.includes('already booked by')) { phase = 'Stopped (another account won)'; phaseClass = 'success'; }
    if (t.includes('verified')) { phase = 'Booking verified'; phaseClass = 'success'; }
    if (t.includes('final results')) { phase = 'Done'; phaseClass = 'success'; }
    if (t.includes('max time') && t.includes('exceeded')) { phase = 'Timed out'; phaseClass = 'danger'; }
  }
  return { phase, phaseClass };
}

function buildAccountCard(account) {
  return `
    <div class="account-card" data-account-id="${escapeHtml(account.id)}">
      <div class="account-header">
        <span class="status-dot" data-role="dot"></span>
        <span class="account-name">${escapeHtml(account.display_name)}</span>
        <span class="badge neutral" data-role="phase" style="background: var(--surface); color: var(--text-muted)">Idle</span>
        <span class="account-meta" data-role="lastupdate">—</span>
      </div>
      <div class="account-body">
        <div class="account-col">
          <div class="account-col-title">Browser view</div>
          <div class="account-screenshot">
            <img data-role="img" style="display:none">
            <div data-role="img-empty" class="account-screenshot-empty">waiting…</div>
          </div>
        </div>
        <div class="account-col">
          <div class="account-col-title">Recent log</div>
          <div class="account-log" data-role="log">—</div>
        </div>
      </div>
    </div>
  `;
}

async function refreshAccounts() {
  try {
    const resp = await fetch('/api/accounts');
    accounts = await resp.json();
    const container = document.getElementById('accounts');
    const empty = document.getElementById('accountsEmpty');
    if (!accounts.length) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    // Rebuild if the set of account IDs changed (covers count changes AND renames)
    const currentIds = [...container.querySelectorAll('.account-card')]
      .map(el => el.dataset.accountId).join(',');
    const newIds = accounts.map(a => a.id).join(',');
    if (currentIds !== newIds) {
      container.innerHTML = accounts.map(buildAccountCard).join('');
    }
  } catch (e) {}
}

async function refreshAccountData() {
  for (const acc of accounts) {
    try {
      const card = document.querySelector(`[data-account-id="${acc.id}"]`);
      if (!card) continue;
      const [logResp, shotResp] = await Promise.all([
        fetch('/api/log/' + acc.id),
        fetch('/api/screenshot_info/' + acc.id),
      ]);
      const logData = await logResp.json();
      const shotData = await shotResp.json();

      // Log
      const logEl = card.querySelector('[data-role="log"]');
      const lines = logData.lines || [];
      if (lines.length) {
        logEl.innerHTML = lines.map(l =>
          `<div class="${classifyLine(l)}">${escapeHtml(l)}</div>`
        ).join('');
        logEl.scrollTop = logEl.scrollHeight;
      } else {
        logEl.textContent = 'No log yet';
      }

      // Phase + status dot
      const {phase, phaseClass} = phaseFromLog(lines);
      const phaseEl = card.querySelector('[data-role="phase"]');
      phaseEl.textContent = phase;
      phaseEl.className = 'badge ' + (phaseClass || 'neutral');
      if (!phaseClass) {
        phaseEl.style.background = 'var(--surface)';
        phaseEl.style.color = 'var(--text-muted)';
      } else {
        phaseEl.style.background = '';
        phaseEl.style.color = '';
      }

      const dotEl = card.querySelector('[data-role="dot"]');
      if (phase === 'Done' || phase === 'Booking verified' || phase === 'Stopped (another account won)') {
        dotEl.className = 'status-dot success';
      } else if (phase === 'Login failed' || phase === 'Timed out') {
        dotEl.className = 'status-dot failed';
      } else if (phase !== 'Idle') {
        dotEl.className = 'status-dot active';
      } else {
        dotEl.className = 'status-dot';
      }

      // Last update
      const lastUpdate = card.querySelector('[data-role="lastupdate"]');
      lastUpdate.textContent = lines.length ? `${lines.length} lines` : '';

      // Screenshot
      const img = card.querySelector('[data-role="img"]');
      const imgEmpty = card.querySelector('[data-role="img-empty"]');
      if (shotData.exists) {
        img.src = '/api/screenshot/' + acc.id + '?t=' + shotData.mtime;
        img.style.display = 'block';
        imgEmpty.style.display = 'none';
      } else {
        img.style.display = 'none';
        imgEmpty.style.display = 'flex';
      }
    } catch (e) {}
  }
}

async function refreshSharedState() {
  try {
    const resp = await fetch('/api/shared_state');
    const state = await resp.json();
    const sat = state.saturday || {};
    const sun = state.sunday || {};
    const satBookings = sat.bookings || [];
    const sunBookings = sun.bookings || [];
    const MAX = 2;  // matches MAX_BOOKINGS_PER_DAY in config.py

    const satEl = document.getElementById('sharedSat');
    const satByEl = document.getElementById('sharedSatBy');
    const sunEl = document.getElementById('sharedSun');
    const sunByEl = document.getElementById('sharedSunBy');

    function fmtDay(bookings) {
      if (!bookings.length) return {value: 'Not booked', cls: 'pending', detail: ''};
      const lines = bookings.map(b => `${b.details} (${b.booked_by})`);
      return {
        value: `${bookings.length}/${MAX} booked`,
        cls: bookings.length >= MAX ? 'booked' : 'pending',
        detail: lines.join(' · '),
      };
    }
    const s = fmtDay(satBookings);
    satEl.textContent = s.value;
    satEl.className = 'shared-value ' + s.cls;
    satByEl.textContent = s.detail;

    const u = fmtDay(sunBookings);
    sunEl.textContent = u.value;
    sunEl.className = 'shared-value ' + u.cls;
    sunByEl.textContent = u.detail;

    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    const totalBooked = satBookings.length + sunBookings.length;
    const totalTarget = 2 * MAX;
    if (satBookings.length >= MAX && sunBookings.length >= MAX) {
      dot.className = 'status-dot success';
      text.textContent = `All ${totalTarget} tee times booked`;
    } else if (totalBooked > 0) {
      dot.className = 'status-dot active';
      text.textContent = `${totalBooked}/${totalTarget} booked — still racing`;
    } else {
      dot.className = 'status-dot';
      text.textContent = 'Idle';
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
  document.getElementById('nextRun').textContent = nextRunSeconds <= 0 ? 'now' : formatDuration(nextRunSeconds);
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
  html += '<th style="width:140px">Booked by</th>';
  html += '</tr></thead><tbody>';
  for (const e of entries) {
    const sat = e.results?.saturday;
    const sun = e.results?.sunday;
    const satOk = !!sat?.success;
    const sunOk = !!sun?.success;
    const satText = satOk ? sat.details : 'No booking';
    const sunText = sunOk ? sun.details : 'No booking';
    let overall, cls;
    if (satOk && sunOk) { overall = 'Success'; cls = 'success'; }
    else if (satOk || sunOk) { overall = 'Partial'; cls = 'warning'; }
    else { overall = 'Failed'; cls = 'danger'; }
    const winners = [];
    if (sat?.booked_by) winners.push('Sat: ' + sat.booked_by);
    if (sun?.booked_by) winners.push('Sun: ' + sun.booked_by);
    const bookedBy = winners.length ? winners.join(', ') : (e.account_name || '—');
    html += '<tr>';
    html += '<td class="muted">' + escapeHtml(formatTimestamp(e.run_started)) + '</td>';
    html += '<td><span class="badge ' + cls + '"><span class="badge-dot"></span>' + overall + '</span></td>';
    html += '<td>' + (satOk ? '<span style="color:var(--success)">✓</span> ' : '<span style="color:var(--danger)">✗</span> ') + escapeHtml(satText) + '</td>';
    html += '<td>' + (sunOk ? '<span style="color:var(--success)">✓</span> ' : '<span style="color:var(--danger)">✗</span> ') + escapeHtml(sunText) + '</td>';
    html += '<td class="muted">' + escapeHtml(bookedBy) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';
  body.innerHTML = html;
}

async function refreshHistory() {
  try {
    const resp = await fetch('/api/history');
    const entries = await resp.json();
    renderHistory(entries);
  } catch (e) {}
}

setInterval(refreshAccounts, 30000);
setInterval(refreshAccountData, 2000);
setInterval(refreshSharedState, 2000);
setInterval(refreshHistory, 10000);
setInterval(tickNextRun, 1000);
setInterval(refreshNextRun, 60000);

(async () => {
  await refreshAccounts();
  refreshAccountData();
  refreshSharedState();
  refreshHistory();
  refreshNextRun();
})();
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/accounts":
            self._json(load_accounts())

        elif path.startswith("/api/log/"):
            account_id = path.split("/", 3)[3]
            lines = read_log_tail(per_account_paths(account_id)["log"], n_lines=30)
            self._json({"lines": lines})

        elif path.startswith("/api/screenshot_info/"):
            account_id = path.split("/", 3)[3]
            p = per_account_paths(account_id)
            info = {"exists": False, "mtime": 0, "label": ""}
            if os.path.exists(p["screenshot"]):
                info["exists"] = True
                info["mtime"] = int(os.path.getmtime(p["screenshot"]))
                try:
                    with open(p["label"], "r") as f:
                        info["label"] = f.read().strip()
                except Exception:
                    pass
            self._json(info)

        elif path.startswith("/api/screenshot/"):
            account_id = path.split("/", 3)[3]
            screenshot_path = per_account_paths(account_id)["screenshot"]
            if not os.path.exists(screenshot_path):
                self.send_response(404); self.end_headers(); return
            try:
                with open(screenshot_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(500); self.end_headers()

        elif path == "/api/shared_state":
            try:
                with open(SHARED_STATE_FILE, "r") as f:
                    raw = f.read().strip()
                    data = json.loads(raw) if raw else {}
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            self._json(data)

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
            self.send_response(404); self.end_headers()

    def _json(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"Austin Tee Time Bot Monitor running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
