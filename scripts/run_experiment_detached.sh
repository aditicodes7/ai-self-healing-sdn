#!/bin/bash
# Runs a Phase 22 experiment inside the VM, DETACHED.
#
# A 6-hour experiment cannot be launched through a plain `multipass exec`:
# the runner becomes a child of that SSH session and is killed when the
# session ends. E2 died that way after exactly one trial, having looked
# healthy the whole time. setsid+nohup detaches it so it survives.
#
# Usage:  ./scripts/run_experiment_detached.sh E2
#         ./scripts/run_experiment_detached.sh E2 --systems P2_ML_RL
set -euo pipefail

EXP="${1:?usage: run_experiment_detached.sh <EXPERIMENT_ID> [extra runner args]}"
shift || true
VM=sdn-vm
REMOTE=/home/ubuntu/ai-self-healing-sdn
LOG="$REMOTE/results/${EXP}_runner.log"

echo "clearing leftover processes in $VM..."
multipass exec "$VM" -- sudo pkill -9 -f 'experiments/runner' 2>/dev/null || true
multipass exec "$VM" -- sudo pkill -9 -f ryu.cmd.manager 2>/dev/null || true
multipass exec "$VM" -- sudo pkill -9 -f uvicorn 2>/dev/null || true
# Daemonised iperf3 servers outlive their Mininet namespace and pile up.
multipass exec "$VM" -- sudo pkill -9 -f 'iperf3 -s' 2>/dev/null || true
multipass exec "$VM" -- sudo mn -c >/dev/null 2>&1 || true

echo "launching $EXP detached; log: $LOG"
multipass exec "$VM" -- sudo bash -c "cd $REMOTE && \
  VENV_ML=$REMOTE/venv-ml VENV_CTRL=/home/ubuntu/venv-ctrl-ai-self-healing-sdn \
  setsid nohup python3 -u experiments/runner.py --experiment $EXP $* \
  > $LOG 2>&1 < /dev/null & echo started"

sleep 5
echo
echo "follow it with:"
echo "  multipass exec $VM -- tail -f $LOG"
