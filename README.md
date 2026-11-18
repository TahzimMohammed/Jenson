# ns-3 offloading kit (week 9: edge queues + first recommenders)

ns-3 simulates the network and the edge nodes; `recommender.py` is the outside process that
picks the edge node. For every task, ns-3 pauses, asks the recommender, sends the task to the
chosen edge, and reports the outcome back once the task has finished or failed.

```
scratch/offload-edge.cc   ns-3 scenario: users -> base station -> edge nodes (with queues + CPUs)
recommender.py            policies: fixed, random, shortest_wait, learner (+ optional TTL)
run.sh                    starts the recommender, then the scenario (one run)
experiments.py            runs the policy comparison sweep, writes summary.csv + charts
Dockerfile                pinned ns-3.48 environment for the whole group
results/                  one CSV per run (one row per task)
```



## Run with Docker (recommended, same for everyone)

```
docker build -t ns3-offload .          # only the last layer rebuilds if you built week 8
docker run --rm -v "$PWD":/work ns3-offload ./run.sh --nUsers=10
docker run --rm -v "$PWD":/work -e POLICY=learner ns3-offload ./run.sh
docker run --rm -v "$PWD":/work ns3-offload python3 experiments.py --jobs 4
```

## Run natively (WSL with ns-3 already built)

```
chmod +x run.sh
NS3_DIR=~/ns-3-dev POLICY=shortest_wait ./run.sh --nUsers=10
NS3_DIR=~/ns-3-dev python3 experiments.py
```

## Recommender settings (environment variables for run.sh)

| Variable | Default | Meaning |
|---|---|---|
| `POLICY` | fixed | `fixed` (always edge 1), `random`, `shortest_wait`, `learner` |
| `TTL` | 0 | seconds a recommendation stays valid per user; 0 = ask every task |
| `SEED` | 1 | passed to ns-3 as `--RngRun` and to the recommender |
| `PORT` | 5555 + `SLURM_ARRAY_TASK_ID` | must differ between simultaneous runs |
| `OUT` | `results/<policy>_ttl<ttl>_seed<seed>.csv` | per-task output |
| `SKIP_BUILD` | 0 | set to 1 to skip copying and rebuilding (parallel runs) |

Policies:
- `fixed`: the week-8 dummy (always edge 1, a slow edge).
- `random`: uniform random edge.
- `shortest_wait`: smallest (queued tasks + 1) / speed. Uses live queue state, so it is the
  "perfect information" heuristic to beat.
- `learner`: collaborative-style. Keeps a success rate per (user, edge), updated from
  feedback as a moving average (`--alpha`), borrows other users' ratings for untried edges,
  explores at random 10% of the time (`--epsilon`). Never sees queue state.

## Scenario options (passed to ns-3)

| Option | Default | Meaning |
|---|---|---|
| `--nUsers` | 10 | user devices |
| `--nEdges` | 3 | edge nodes |
| `--simTime` | 30 | simulated seconds of task generation |
| `--meanInterval` | 0.3 | mean seconds between tasks, per user (load) |
| `--taskSize` | 1000 | bytes per task (64 to 1400, one packet) |
| `--taskWork` | 0.1 | mean Gcycles per task (each task uniform 0.5x to 1.5x) |
| `--deadline` | 0.2 | seconds |
| `--edgeGhz` | 4,1,1 | CPU speed per edge; the last value repeats for extra edges |
| `--queueCap` | 5 | max waiting tasks per edge |
| `--dropPolicy` | tail | `tail` or `oldest` |
| `--lossRate` | 0 | packet loss probability on edge links |
| `--RngRun` | (from SEED) | independent random run number |

With the defaults the edges can serve about 40 + 10 + 10 = 60 tasks/s. Ten users at
`--meanInterval=0.3` offer about 33 tasks/s.

## Output columns (one row per task)

`policy, ttl, seed, task_id, user, t, work, deadline, queues` (queue lengths when the decision
was made), `edge` (chosen), `outcome, met_deadline, delay` (total, met tasks only),
`net_delay, queue_wait, edge_load` (tasks at that edge when the outcome happened).

## HPC (Apptainer)

`apptainer build ns3.sif docker-daemon://ns3-offload:latest`. `/opt/ns-3` is read-only under
Apptainer, so compile the scratch code into the image first (or keep a copy of ns-3 in your
home directory), then run with `SKIP_BUILD=1`. Array jobs get distinct ports automatically
through `SLURM_ARRAY_TASK_ID`. Not yet tested on the cluster.

## Not modelled yet (next steps)

Wireless links and mobility, retransmissions, tasks bigger than one packet, processor sharing
at edges, stale/delayed edge-state broadcasts.
