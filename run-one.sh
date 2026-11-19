#!/usr/bin/env bash
# One isolated simulation run, safe to run many in parallel on the same machine.
#
#   POLICY=learner TTL=1 SEED=3 run-one.sh OUT_DIR [scenario args...]
#   e.g. POLICY=fastest SEED=2 run-one.sh results/runs/0007 --meanInterval=0.2
#
# Differences from run.sh (which is for development):
#   - runs the already-compiled offload-edge binary, so it works inside a read-only
#     Apptainer image on the cluster (no ./ns3 run, no compiling at run time)
#   - picks a free TCP port, so parallel runs on one node don't collide
#   - writes everything for this run into OUT_DIR:
#       args.txt         policy, ttl, seed and the scenario arguments
#       feedback.csv     one row per task (from the recommender)
#       summary.txt      ns-3's end-of-run summary (tasks generated, outcomes)
#       ns3.log, recommender.log
#
# Env vars (same meaning as for run.sh):
#   POLICY  fixed | random | shortest_wait | learner | fastest   (default fixed)
#   TTL     seconds a recommendation stays valid, 0 = ask every task (default 0)
#   SEED    ns-3 --RngRun and recommender seed (default 1)
#   OFFLOAD_SIM  compiled scenario (default /usr/local/bin/offload-sim, set in the image;
#                natively falls back to the offload-edge binary under $NS3_DIR/build)
set -euo pipefail

OUT="${1:?usage: run-one.sh OUT_DIR [scenario args...]}"
shift
APP="${OFFLOAD_APP:-$(cd "$(dirname "$0")" && pwd)}"    # folder with recommender.py
POLICY="${POLICY:-fixed}"
TTL="${TTL:-0}"
SEED="${SEED:-1}"

SIM="${OFFLOAD_SIM:-/usr/local/bin/offload-sim}"
if [ ! -x "$SIM" ] && [ -n "${NS3_DIR:-}" ]; then        # native: the binary run.sh built
    SIM="$(ls "$NS3_DIR"/build/scratch/ns3*-offload-edge-* 2>/dev/null | head -1 || true)"
fi
[ -n "$SIM" ] && [ -x "$SIM" ] || {
    echo "offload-edge binary not found (run ./run.sh once, or set OFFLOAD_SIM)" >&2
    exit 1
}

mkdir -p "$OUT"
echo "POLICY=$POLICY TTL=$TTL SEED=$SEED $*" > "$OUT/args.txt"

# Ask the OS for a free port. Tiny race if another run grabs it first; ns-3 retries the
# connection for ~10 s, and the recommender would fail to bind (see recommender.log).
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')

python3 "$APP/recommender.py" --policy "$POLICY" --ttl "$TTL" --seed "$SEED" \
    --port "$PORT" --log "$OUT/feedback.csv" > "$OUT/recommender.log" 2>&1 &
REC=$!
trap 'kill $REC 2>/dev/null || true' EXIT

"$SIM" --port="$PORT" --RngRun="$SEED" "$@" > "$OUT/summary.txt" 2> "$OUT/ns3.log"
wait $REC
