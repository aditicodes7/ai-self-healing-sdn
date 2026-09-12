#!/bin/bash
# Keep the VM-side experiment chain alive across VM crashes.
#
# The VM died hard mid-run (no clean shutdown, last kernel message
# "htb: too many events!"), which killed the chain and left the queue
# stalled with nobody watching. Multipass brings the instance back but
# nothing restarts the work inside it.
#
# Every VM command goes through vm_exec.sh, so a wedged multipass client
# is killed at a deadline instead of spinning at 100% CPU next to the
# experiment it is supposed to be observing (that mistake cost 30
# baseline trials once already).
#
# Safe to run alongside the chain: it only ever relaunches when no
# chain.sh is running AND the chain has not reported ALL DONE, and
# chain.sh itself skips experiments that already recorded a done marker.
set -uo pipefail
cd "$(dirname "$0")/.."
REMOTE=/home/ubuntu/ai-self-healing-sdn
INTERVAL=${INTERVAL:-300}

while true; do
  if ! multipass info sdn-vm 2>/dev/null | grep -q "State:.*Running"; then
    echo "[$(date +%H:%M)] VM not running -- starting it"
    multipass start sdn-vm >/dev/null 2>&1 || true
    sleep 45
  fi

  if ./scripts/vm_exec.sh -t 40 -- grep -q "ALL DONE" "$REMOTE/results/chain.log" 2>/dev/null; then
    echo "[$(date +%H:%M)] chain reported ALL DONE"
    exit 0
  fi

  if ! ./scripts/vm_exec.sh -t 40 -- pgrep -f "bash chain.sh" >/dev/null 2>&1; then
    echo "[$(date +%H:%M)] chain not running -- relaunching (finished experiments are skipped)"
    ./scripts/vm_exec.sh -t 50 -- sudo bash -c \
      "cd $REMOTE && setsid nohup bash chain.sh >> results/chain.log 2>&1 < /dev/null & echo ok" \
      >/dev/null 2>&1 || true
  fi

  sleep "$INTERVAL"
done
