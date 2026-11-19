#!/bin/bash
# One Slurm array task = one line of runs.csv = one simulation run.
#
# From the project folder on Aire (ns3-offload.sif next to it, see docs/BIG-RUNS.md):
#   python3 experiments.py --plan                          # writes runs.csv
#   mkdir -p logs
#   sbatch --array=1-$(( $(wc -l < runs.csv) - 1 ))%50 slurm/sweep.sbatch
#   squeue -u $USER                                        # watch progress
#   python3 experiments.py --collect                       # when all are done
#
# "%50" caps how many run at once, to be fair to other users.
#SBATCH --job-name=offload-sweep
#SBATCH --time=00:30:00
#SBATCH --cpus-per-task=1
#SBATCH --mem-per-cpu=2G
#SBATCH --output=logs/%A_%a.out

set -euo pipefail
module load apptainer/1.3.6

SIF="${SIF:-$PWD/ns3-offload.sif}"
RESULTS="${RESULTS:-$PWD/results/runs}"
mkdir -p "$RESULTS"

# runs.csv columns: run_id,policy,ttl,seed,args. Line 1 is the header, so array task N
# reads line N+1. args never contains commas (experiments.py --plan checks this).
LINE=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" runs.csv)
IFS=, read -r RUN_ID POLICY TTL SEED ARGS <<< "$LINE"

echo "run $RUN_ID on $(hostname): POLICY=$POLICY TTL=$TTL SEED=$SEED $ARGS"
# shellcheck disable=SC2086  # ARGS is intentionally split into separate arguments
apptainer exec --bind "$RESULTS:/results" \
    --env "POLICY=$POLICY" --env "TTL=$TTL" --env "SEED=$SEED" "$SIF" \
    /opt/offload/run-one.sh "/results/$RUN_ID" $ARGS
