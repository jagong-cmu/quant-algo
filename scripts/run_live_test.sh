#!/bin/bash
# One-shot wrapper: fired by launchd at the market open to run the single
# 1-contract live execution test, then removes its own LaunchAgent so it never
# fires again. LIVE submission is enabled only inside live_test_order.py --live,
# which itself refuses to fire outside market hours.
REPO="/Users/jonathang/Desktop/GitHub/quant-algo"
PLIST="$HOME/Library/LaunchAgents/com.quantalgo.livetest.plist"

cd "$REPO" || exit 1
mkdir -p logs
{
  echo "=================================================="
  echo "launchd live test fired: $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
  echo "=================================================="
  ./.venv/bin/python live_test_order.py --live --haircut 0.15
  echo "exit code: $?"
} >> logs/launchd_livetest.out 2>&1

# one-shot self-destruct so it cannot recur
launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
