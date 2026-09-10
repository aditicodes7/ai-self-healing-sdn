#!/bin/bash
# Run one command in the VM with a HARD host-side deadline.
#
# Use this for every VM command instead of a bare `multipass exec`.
#
# Why: a `multipass exec` client can wedge and then spin at ~100% CPU on
# the host indefinitely, long after the remote command has finished. It
# is not a hang -- the process burns a full core. During the first E2
# attempt three wedged clients (a status poll, a `tail -f` monitor, and a
# completion watcher) took 3 of this Mac's 8 cores for three hours, and
# qemu's share went from 246% to 34.6%: the VM ran the entire baseline
# arm on roughly a seventh of the CPU it should have had. Because the
# starvation ended partway through the run, it was aligned with the
# variable under test, so those 30 baseline trials had to be discarded
# (results/E2_runner_DISCARDED_cpu_starved.log).
#
# macOS ships no `timeout`, hence the explicit watchdog below. The kill
# is unconditional: a client that has served its output and not exited
# is exactly the failure mode this guards against.
#
# NEVER hold a long-lived `multipass exec` (no `tail -f`, no `until`
# poll loop around one). Poll infrequently with this instead.
#
# Usage:  ./scripts/vm_exec.sh [-t SECONDS] -- <command...>
#         ./scripts/vm_exec.sh -- tail -5 /home/ubuntu/.../E2_runner.log
# Exit:   the command's own status, or 124 if the deadline was hit.
set -uo pipefail

VM=sdn-vm
DEADLINE=45
if [ "${1:-}" = "-t" ]; then DEADLINE="$2"; shift 2; fi
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "usage: vm_exec.sh [-t SECONDS] -- <command...>" >&2; exit 2; }

OUT=$(mktemp)
multipass exec "$VM" -- "$@" > "$OUT" 2>&1 < /dev/null &
PID=$!

WAITED=0
while kill -0 "$PID" 2>/dev/null; do
  if [ "$WAITED" -ge "$DEADLINE" ]; then
    kill -9 "$PID" 2>/dev/null
    wait "$PID" 2>/dev/null
    cat "$OUT"; rm -f "$OUT"
    echo "vm_exec: killed wedged client after ${DEADLINE}s" >&2
    exit 124
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done

wait "$PID"; STATUS=$?
cat "$OUT"; rm -f "$OUT"
exit "$STATUS"
