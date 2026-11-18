#!/usr/bin/env bash
# Starts the recommender, then runs the ns-3 scenario against it.
#
#   ./run.sh --nUsers=10 --meanInterval=0.3          scenario options are passed to ns-3
#   POLICY=learner SEED=3 ./run.sh                    recommender settings via env vars
#
# Env vars:
#   POLICY      fixed | random | shortest_wait | learner   (default fixed)
#   TTL         seconds a recommendation stays valid, 0 = ask every task (default 0)
#   SEED        run number: ns-3 --RngRun and recommender seed (default 1)
#   PORT        TCP port, must differ between runs on one machine
#               (default 5555 + SLURM_ARRAY_TASK_ID, so HPC array jobs don't collide)
#   OUT         feedback CSV path (default results/<policy>_ttl<ttl>_seed<seed>.csv)
#   SKIP_BUILD  1 = don't copy/rebuild scratch code (use when running many jobs at once)
#   NS3_DIR     ns-3 folder (Docker sets /opt/ns-3; natively defaults to ~/ns-3-dev)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NS3_DIR="${NS3_DIR:-$HOME/ns-3-dev}"
POLICY="${POLICY:-fixed}"
TTL="${TTL:-0}"
SEED="${SEED:-1}"
PORT="${PORT:-$((5555 + ${SLURM_ARRAY_TASK_ID:-0}))}"
OUT="${OUT:-$HERE/results/${POLICY}_ttl${TTL}_seed${SEED}.csv}"
mkdir -p "$(dirname "$OUT")"

if [ "${SKIP_BUILD:-0}" != 1 ]; then
    cp "$HERE"/scratch/*.cc "$NS3_DIR/scratch/"
    (cd "$NS3_DIR" && ./ns3 build offload-edge)
fi

python3 "$HERE/recommender.py" --policy "$POLICY" --ttl "$TTL" --seed "$SEED" \
    --port "$PORT" --log "$OUT" &
REC=$!
trap 'kill $REC 2>/dev/null || true' EXIT

cd "$NS3_DIR"
./ns3 run --no-build "offload-edge --port=$PORT --RngRun=$SEED $*"
wait $REC
