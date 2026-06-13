#!/usr/bin/env python3
"""Local control + observability dashboard for the autonomous runner.

A dependency-free (stdlib http.server) web UI that lets you SEE what the system
is doing and MANAGE it, without touching the runner process:

  * status: mode (PAPER/LIVE), market open, equity, day P/L, kill switch
  * open positions: strikes, expiry, DTE, entry credit, live mark, profit %, and
    each position's management note
  * action-required ALERTS: spreads the runner decided to close but could not
    submit (paper/dry-run or a live error) -- i.e. "trades to close" you must
    handle manually, with the exact unwind legs
  * recent operations: a tail of the latest runner log
  * config snapshot: the live management thresholds

It is a SAFE controller: it never talks to the broker. Buttons enqueue commands
into state/commands.json, which the running runner drains each cycle (the runner
stays the single execution path). Halt/Resume, request-Close, Mark-closed
(remove), and Ack-alert all flow through that one queue.

Run:
    python dashboard.py                 # http://127.0.0.1:8787
    PP_DASH_PORT=9000 python dashboard.py
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Point at wherever the runner writes its state/logs. Defaults to this script's
# directory; set PP_ROOT to watch a runner running from another checkout.
ROOT = os.environ.get("PP_ROOT") or os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(ROOT, "state", "auto_ledger.json")
ALERTS_PATH = os.path.join(ROOT, "state", "alerts.json")
COMMANDS_PATH = os.path.join(ROOT, "state", "commands.json")
LOG_DIR = os.path.join(ROOT, "logs")


# ---- state assembly --------------------------------------------------------
def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path))
    except (json.JSONDecodeError, OSError):
        return default


def _latest_log_tail(n: int = 80) -> list[str]:
    if not os.path.isdir(LOG_DIR):
        return []
    logs = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
            if f.startswith("pp_options_autorun_") and f.endswith(".log")]
    if not logs:
        logs = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR) if f.endswith(".log")]
    if not logs:
        return []
    latest = max(logs, key=os.path.getmtime)
    try:
        with open(latest, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
    except OSError:
        return []


def build_state() -> dict:
    ledger = _read_json(LEDGER_PATH, {})
    alerts = _read_json(ALERTS_PATH, {}).get("alerts", [])
    return {
        "meta": ledger.get("meta", {}),
        "open": ledger.get("open", []),
        "alerts": [a for a in alerts if not a.get("acknowledged")],
        "log": _latest_log_tail(),
    }


def enqueue(cmd: dict) -> None:
    os.makedirs(os.path.dirname(COMMANDS_PATH), exist_ok=True)
    data = _read_json(COMMANDS_PATH, {"commands": []})
    if not isinstance(data, dict) or "commands" not in data:
        data = {"commands": []}
    data["commands"].append(cmd)
    json.dump(data, open(COMMANDS_PATH, "w"), indent=2)


# ---- HTTP handler ----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):   # silence default per-request stderr logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(build_state()).encode(), "application/json")
        elif self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/command":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            cmd = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
            return
        action = cmd.get("action")
        if action not in ("halt", "resume", "close", "remove", "ack"):
            self._send(400, b'{"ok":false,"error":"unknown action"}', "application/json")
            return
        enqueue({k: v for k, v in cmd.items() if k in ("action", "symbol", "id")})
        self._send(200, b'{"ok":true,"queued":true}', "application/json")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PentPort Runner</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
        --grn:#3fb950;--red:#f85149;--amb:#d29922;--blu:#58a6ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
         padding:14px 20px;border-bottom:1px solid var(--bd);background:var(--card)}
  h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.5px}
  .wrap{padding:18px 20px;max-width:1180px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
  .stat{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}
  .stat .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
  .stat .v{font-size:18px;font-weight:700;margin-top:3px}
  .badge{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700}
  .b-live{background:rgba(248,81,73,.15);color:var(--red);border:1px solid var(--red)}
  .b-paper{background:rgba(88,166,255,.12);color:var(--blu);border:1px solid var(--blu)}
  .b-ok{background:rgba(63,185,80,.13);color:var(--grn);border:1px solid var(--grn)}
  .b-halt{background:rgba(210,153,34,.15);color:var(--amb);border:1px solid var(--amb)}
  h2{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;
     margin:22px 0 8px}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--bd);border-radius:8px;overflow:hidden}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase}
  tr:last-child td{border-bottom:none}
  .pos{color:var(--grn)} .neg{color:var(--red)}
  .alert{background:rgba(248,81,73,.08);border:1px solid var(--red);border-radius:8px;
         padding:12px 14px;margin-bottom:10px}
  .alert .t{font-weight:700;color:var(--red)}
  .alert .d{color:var(--fg);margin:4px 0 8px;white-space:pre-wrap}
  .legs{color:var(--mut);font-size:12px;margin-bottom:8px}
  button{background:#21262d;color:var(--fg);border:1px solid var(--bd);border-radius:6px;
         padding:5px 11px;cursor:pointer;font:inherit;font-size:12px}
  button:hover{border-color:var(--blu)}
  button.danger:hover{border-color:var(--red);color:var(--red)}
  .log{background:#010409;border:1px solid var(--bd);border-radius:8px;padding:10px 12px;
       font-size:12px;max-height:320px;overflow:auto;white-space:pre-wrap;color:#b9c0c8}
  .muted{color:var(--mut)} .empty{color:var(--mut);padding:10px 2px}
  #upd{color:var(--mut);font-size:12px;margin-left:auto}
</style></head>
<body>
<header>
  <h1>PENTPORT&nbsp;RUNNER</h1>
  <span id="mode" class="badge b-paper">--</span>
  <span id="market" class="badge">--</span>
  <span id="halt" class="badge"></span>
  <button id="haltBtn" onclick="toggleHalt()">Halt</button>
  <span id="upd"></span>
</header>
<div class="wrap">
  <div class="grid" id="stats"></div>

  <h2>Action required <span class="muted" id="alertCount"></span></h2>
  <div id="alerts"></div>

  <h2>Open positions</h2>
  <div id="positions"></div>

  <h2>Recent operations</h2>
  <div class="log" id="log"></div>

  <h2>Config</h2>
  <div class="log" id="config"></div>
</div>
<script>
let HALTED=false;
const f2=x=>(x==null?'--':Number(x).toFixed(2));
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function cmd(action,extra={}){
  await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({action},extra))});
  setTimeout(refresh,250);
}
const toggleHalt=()=>cmd(HALTED?'resume':'halt');
const closePos=s=>{if(confirm('Request close of '+s+'?'))cmd('close',{symbol:s})};
const removePos=s=>{if(confirm('Mark '+s+' as closed and drop it from the book?'))cmd('remove',{symbol:s})};
const ackAlert=id=>cmd('ack',{id});

function pct(x){if(x==null)return '<span class="muted">--</span>';
  const c=x>=0?'pos':'neg';return '<span class="'+c+'">'+(x>=0?'+':'')+x.toFixed(1)+'%</span>';}

function render(s){
  const m=s.meta||{};
  HALTED=!!m.halted;
  const mode=document.getElementById('mode');
  mode.textContent=m.mode||'--'; mode.className='badge '+(m.mode==='LIVE'?'b-live':'b-paper');
  const mk=document.getElementById('market');
  mk.textContent=m.market_open?'MARKET OPEN':'MARKET CLOSED';
  mk.className='badge '+(m.market_open?'b-ok':'b-halt');
  const h=document.getElementById('halt');
  h.textContent=HALTED?'HALTED':''; h.className='badge '+(HALTED?'b-halt':'');
  document.getElementById('haltBtn').textContent=HALTED?'Resume':'Halt';
  document.getElementById('upd').textContent=m.updated?('updated '+m.updated):'';

  document.getElementById('stats').innerHTML=[
    ['Equity', m.equity!=null?('$'+Number(m.equity).toLocaleString()):'--'],
    ['Day P/L', pct(m.day_pl_pct)],
    ['Open spreads', (s.open||[]).length],
    ['Alerts', (s.alerts||[]).length],
  ].map(([k,v])=>`<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  // alerts
  const A=s.alerts||[];
  document.getElementById('alertCount').textContent=A.length?('('+A.length+')'):'';
  document.getElementById('alerts').innerHTML = A.length ? A.map(a=>{
    const legs=(a.legs||[]).map(l=>`${l.instruction} ${l.symbol} x${l.quantity} @ ${f2(l.limit)}`).join('  •  ');
    const sym=(a.legs&&a.legs[0])?a.legs[0].symbol:null;
    return `<div class="alert"><div class="t">⚠ ${esc(a.title)}</div>
      <div class="d">${esc(a.detail)}</div>
      ${legs?`<div class="legs">${esc(legs)}</div>`:''}
      ${sym?`<button class="danger" onclick="removePos('${sym}')">Mark closed</button> `:''}
      <button onclick="ackAlert('${a.id}')">Dismiss</button></div>`;
  }).join('') : '<div class="empty">None — nothing needs your attention.</div>';

  // positions
  const P=s.open||[];
  document.getElementById('positions').innerHTML = P.length ? `<table><thead><tr>
    <th>Underlying</th><th>Short/Long</th><th>Expiry</th><th>DTE</th><th>Qty</th>
    <th>Credit</th><th>Mark</th><th>Profit</th><th>Status</th><th></th></tr></thead><tbody>`+
    P.map(p=>{
      const dte=Math.round((new Date(p.expiry)-new Date())/864e5);
      const pf=p.profit_frac!=null?(p.profit_frac*100):null;
      const pend=p.close_pending?' style="background:rgba(248,81,73,.06)"':'';
      return `<tr${pend}><td>${esc(p.underlying)}</td>
        <td>${f2(p.short_k)}/${f2(p.long_k)}P</td><td>${esc(p.expiry)}</td><td>${dte}</td>
        <td>${p.contracts}</td><td>${f2(p.entry_credit)}</td><td>${f2(p.mark)}</td>
        <td>${pf==null?'<span class="muted">--</span>':('<span class="'+(pf>=0?'pos':'neg')+'">'+pf.toFixed(0)+'%</span>')}</td>
        <td class="muted" style="text-align:left;white-space:normal">${esc(p.manage_note||'')}</td>
        <td><button onclick="closePos('${p.short_sym}')">Close</button>
            <button class="danger" onclick="removePos('${p.short_sym}')">Remove</button></td></tr>`;
    }).join('')+'</tbody></table>'
    : '<div class="empty">No open spreads.</div>';

  document.getElementById('log').textContent=(s.log||[]).join('\n')||'(no log yet)';
  const c=m.config||{};
  document.getElementById('config').textContent=Object.keys(c).length?
    Object.entries(c).map(([k,v])=>k+' = '+v).join('\n'):'(runner has not written config yet)';
}

async function refresh(){
  try{const r=await fetch('/api/state');render(await r.json());}
  catch(e){document.getElementById('upd').textContent='disconnected';}
}
refresh(); setInterval(refresh,4000);
</script>
</body></html>"""


def main() -> int:
    port = int(os.environ.get("PP_DASH_PORT", "8787"))
    host = os.environ.get("PP_DASH_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"PentPort runner dashboard -> http://{host}:{port}  (Ctrl-C to stop)")
    print(f"  ledger : {LEDGER_PATH}")
    print(f"  alerts : {ALERTS_PATH}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
