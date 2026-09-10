#!/bin/bash
# Runs a Phase 22 experiment inside the VM, DETACHED.
#
# A 6-hour experiment cannot be launched through a plain `multipass exec`:
# the runner becomes a child of that SSH session and is killed when the
# session ends. E2 died that way after exactly one trial, having looked
# healthy the whole time. setsid+nohup detaches it so it survives.
#
# Every VM-side step runs inside ONE `multipass exec`, and each step is
# wrapped in the VM's own `timeout`. Both details are load-bearing: a
# `multipass exec sdn-vm -- sudo mn -c` was once seen spinning at 100%
# CPU on the host for 17 minutes AFTER the cleanup had already finished
# inside the VM (an idle VM, no bridges, no mn process) -- the client
# never got EOF on the channel. macOS ships no `timeout`, so the guard
# has to live on the Linux side, and fewer channels means fewer chances
# to wedge. If a step does exceed its budget it is skipped, not fatal:
# cleanup is best-effort, and refusing to launch a 6-hour run because a
# stale interface would not delete is the wrong trade.
#
# Usage:  ./scripts/run_experiment_detached.sh E2
#         ./scripts/run_experiment_detached.sh E2 --systems P2_ML_RL
set -euo pipefail

EXP="${1:?usage: run_experiment_detached.sh <EXPERIMENT_ID> [extra runner args]}"
shift || true
VM=sdn-vm
REMOTE=/home/ubuntu/ai-self-healing-sdn
LOG="$REMOTE/results/${EXP}_runner.log"

echo "cleaning up and launching $EXP in $VM..."
multipass exec "$VM" -- sudo bash -c "
  set -u
  # Never use a bare \`pkill -f\`, here or anywhere in this block.
  # It matches against every full command line including this shell's
  # own, and this whole script body IS that command line -- so any
  # pattern spelled anywhere in it (the loop, a comment explaining the
  # loop, or the launch line below, which names the runner by path)
  # makes the cleanup kill the shell that is about to launch the run:
  # exit 255, silently, before the launch line is ever reached. That
  # cost three attempts and a bracket-escaping trick that broke again
  # the moment a comment mentioned a pattern. Skipping our own PID and
  # our sudo parent fixes it outright and cannot be re-broken by
  # editing the text around it.
  # iperf3 is in the list because its servers are daemonised: they
  # outlive their Mininet namespace and pile up across runs.
  for pat in 'experiments/runner' 'ryu.cmd.manager' 'uvicorn' 'iperf3 -s'; do
    for pid in \$(timeout 20 pgrep -f \"\$pat\" || true); do
      [ \"\$pid\" = \"\$\$\" ] && continue
      [ \"\$pid\" = \"\$PPID\" ] && continue
      kill -9 \"\$pid\" 2>/dev/null || true
    done
  done
  timeout 120 mn -c >/dev/null 2>&1 || echo 'WARN: mn -c did not finish in 120s; continuing'
  cd $REMOTE
  VENV_ML=$REMOTE/venv-ml VENV_CTRL=/home/ubuntu/venv-ctrl-ai-self-healing-sdn \
    setsid nohup python3 -u experiments/runner.py --experiment $EXP $* \
    > $LOG 2>&1 < /dev/null &
  echo started
" < /dev/null

echo
echo "follow it with:"
echo "  multipass exec $VM -- tail -f $LOG"
