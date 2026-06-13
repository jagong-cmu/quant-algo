# Deploying the Autonomous Runner (LIVE)

This package lets the runner trade **autonomously on a server you control**, so it
keeps running without your Mac. You provision the box and hold the key; the code
is deployment-ready.

> ## ⚠️ Read before you deploy live
> This will place **real orders unattended.** Be clear-eyed about what it is:
> - **No demonstrated edge.** Every backtest return in this project was a
>   Black-Scholes modeling artifact (e.g. one 0DTE trade = half the "profit";
>   the same paper day re-priced from +$2.9k to +$7.7k).
> - **Fills are unreliable** — PentPort exposes no option bid/ask, so leg prices
>   come off `lastPrice`; spreads may not complete and end up **long-only**.
> - **It has never completed a validated live-hours session.**
> - The one real order placed during development **legged out** and needed manual
>   attention (since fixed by the safe executor, but it shows the venue's quirks).
>
> **Strong recommendation:** run it in **PAPER** first (below) for several
> sessions and read the logs before enabling live. Start with the smallest size.

## Safety rails that stay ON in live mode
- **Protective-leg-first executor** — opens the long (defined-risk) leg, confirms
  it's held, only then the short. A leg-out can never leave you naked short.
- **Hard guardrails** — 3%/trade and 20%/book caps run before every entry.
- **Daily-loss kill switch** — halts new entries after −5% on the day.
- **Market-hours gating** — entries only 13:30–20:00 UTC, Mon–Fri.
- **Position lifecycle** — open spreads are closed at a DTE threshold (safe unwind).
- **State ledger** — open positions survive restarts/reboots.

## What it trades (v1)
SPY put credit spreads, ~30-delta short / $5-wide, **1 contract**, one concurrent
spread. Tune in `pp_options/runner.py` (`AUTO_*`, `EXIT_DTE`, `DAILY_LOSS_HALT_PCT`).

---

## Option A — Docker (recommended)
On your server (a small VPS is plenty):
```bash
git clone <your repo> quant-algo && cd quant-algo
printf 'PENTPORT_API_KEY=pp_live_YOURKEY\n' > .env   # server-side secret, gitignored
# PAPER first (no real orders):
docker compose run --rm runner python autorun.py --daemon
# When satisfied, go live (this is the default CMD):
docker compose up -d --build
docker compose logs -f          # watch it
docker compose down             # stop / kill switch
```

## Option B — systemd (no Docker)
```bash
sudo rsync -a ./ /opt/quant-algo/        # deploy code
cd /opt/quant-algo && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo install -m 600 /dev/stdin /etc/quantalgo.env <<<'PENTPORT_API_KEY=pp_live_YOURKEY'
sudo useradd -r -s /usr/sbin/nologin trader && sudo chown -R trader /opt/quant-algo
sudo cp deploy/quantalgo.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now quantalgo
journalctl -u quantalgo -f       # watch it
sudo systemctl stop quantalgo    # stop / kill switch
```

## Operating it
- **Logs:** `logs/` (container/host volume) or `journalctl -u quantalgo`.
- **Open positions / kill-switch state:** `state/auto_ledger.json`.
- **Emergency stop:** `docker compose down` or `systemctl stop quantalgo`. (Note:
  PentPort has **no cancel API** — a stopped daemon won't pull a resting order;
  cancel any working order in the PentPort web UI. Open positions persist and
  must be closed manually or by restarting the daemon, which resumes lifecycle
  management.)

## Key handling
- The key is read from `PENTPORT_API_KEY` only. It is **never** baked into the
  image (`.dockerignore` excludes `.env`) or committed. Keep `.env` / the
  systemd EnvironmentFile `600` and off version control.

## Time zone
The runner gates on **UTC** market hours (13:30–20:00 ≈ US 9:30–16:00 ET during
EDT). Run the server in UTC, or it still works — gating is UTC-internal.
