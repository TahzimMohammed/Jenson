# Running experiments at scale (Docker → Apptainer → Aire)

## The idea

One experiment is many runs: every policy × load × TTL × decision delay, repeated over
several seeds. Each run is small and independent, so we run them side by side: a few at a time
on a laptop, hundreds at a time on Aire.

The same container image is used everywhere, so a run on Aire is exactly the run you tested
locally.

```
 experiments.py --plan  ──►  runs.csv  (one line per run: id, policy, ttl, seed, ns-3 args)
                                │
         ┌──────────────────────┴───────────────────────┐
  laptop: experiments.py --run-plan            Aire: slurm/sweep.sbatch
  (N runs in parallel, native or --docker)     (one Slurm array task per line)
         └──────────────────────┬───────────────────────┘
                                ▼
         results/runs/<id>/  (args.txt, feedback.csv, summary.txt, ns3.log, recommender.log)
                                │
 experiments.py --collect  ──►  results/sweep/summary.csv + charts (same as a local sweep)
```

`--plan` uses the same run list as a plain `python3 experiments.py`, and `--collect` feeds the
same analysis, so both routes give the same `summary.csv` (checked: byte-identical on ns-3.49).
To change the experiment, edit `POLICIES`, `INTERVALS`, `TTLS`, `DELAYS` at the top of
`experiments.py`; `--seeds N` sets the repeats.

## Two ways to run one simulation

| | `run.sh` | `run-one.sh` |
|---|---|---|
| For | developing the scenario | experiments |
| Compiles? | yes, unless `SKIP_BUILD=1` (`./ns3 run`) | no, runs the prebuilt `offload-sim` binary |
| Port | `PORT` (5555 + array id by default) | asks the OS for a free port |
| Output | one CSV (`OUT`) | its own folder per run |
| Works in a read-only Apptainer image | no | yes |

Both take `POLICY`, `TTL`, `SEED` as environment variables and ns-3 options as arguments:
```bash
POLICY=learner TTL=1 SEED=3 ./run-one.sh results/runs/test --meanInterval=0.3 --decisionDelay=0.02
```
Natively, `run-one.sh` uses the `offload-edge` binary under `$NS3_DIR/build/scratch/`
(build it once with `./run.sh` or `python3 experiments.py`).

## 1. Build the image (Mac or Linux, Docker Desktop running)

```bash
docker build -t ns3-offload .               # first time 15–30 min; then ~1 min after code changes
docker run --rm ns3-offload offload-sim --PrintHelp      # check the scenario is inside
```
The image builds ns-3.49 with `--build-profile=optimized`, without examples or tests, and only
the modules `offload-edge.cc` needs. It compiles `scratch/offload-edge.cc` into the image and
links it as `/usr/local/bin/offload-sim`; `recommender.py` and `run-one.sh` live in `/opt/offload`.

Docker Desktop on an 8 GB Mac: give Docker at least 4 GB of memory (Settings → Resources) and
build with `--build-arg JOBS=2`, or `JOBS=1` if it stalls.

When you change `scratch/*.cc`, `recommender.py` or `run-one.sh`, rebuild the image. Only the
last layers rebuild, because the ns-3 build is cached.

## 2. Try the sweep on your laptop

```bash
python3 experiments.py --plan
python3 experiments.py --run-plan --jobs 4                       # native ns-3 build
python3 experiments.py --run-plan --jobs 4 --docker ns3-offload  # or inside the image
python3 experiments.py --collect                                 # → results/sweep/summary.csv
```
Reference: the full plan (185 runs) takes about 20 s natively and 85 s with `--docker`
(container start-up per run), 4 jobs on an M2.

## 3. Get the image onto Aire

Aire runs x86-64 processors and a Mac is ARM, so the cluster needs an x86-64 build.

**Option A (recommended): GitHub builds it.** `.github/workflows/container.yml` builds the
linux/amd64 image on every push to `main` that touches the Dockerfile, `scratch/`,
`recommender.py` or `run-one.sh` (or by hand: Actions → container → Run workflow), and publishes
it to GitHub Container Registry. Then, on Aire:
```bash
module load apptainer/1.3.6
apptainer pull ns3-offload.sif docker://ghcr.io/tahzimmohammed/jenson:latest
```
(If the repository is private, either make the package public in GitHub's package settings, or use
`apptainer pull --docker-login …` with a GitHub token.)

**Option B: build on your Mac and copy it.** Slow, because the Mac emulates x86-64:
```bash
docker buildx build --platform linux/amd64 -t ns3-offload:amd64 --load .
docker save ns3-offload:amd64 -o ns3-offload-amd64.tar
scp ns3-offload-amd64.tar <user>@<aire-login-node>:<project-folder>/
# on Aire:
module load apptainer/1.3.6
apptainer build ns3-offload.sif docker-archive://ns3-offload-amd64.tar
```

## 4. Run the sweep on Aire

```bash
# in the project folder on Aire (git clone the repo there; put ns3-offload.sif next to it)
python3 experiments.py --plan
mkdir -p logs
sbatch --array=1-$(( $(wc -l < runs.csv) - 1 ))%50 slurm/sweep.sbatch
squeue -u $USER                     # watch; failed runs leave details in logs/ and results/runs/<id>/
python3 experiments.py --collect    # when finished; copy results/sweep/summary.csv back
```
- `%50` limits the job to 50 runs at a time.
- Each run asks for 1 core, 2 GB of memory and 30 minutes (a run takes well under a second now,
  so this is generous). Adjust in `slurm/sweep.sbatch` once real runs show what they need.
- For big sweeps, put the run folders on scratch storage
  (`RESULTS=$SCRATCH/offload-runs sbatch …`), then point `--collect` at them (or symlink
  `results/runs` there).
- `--collect` only needs Python's standard library for `summary.csv`; charts need matplotlib.

## Gotchas

- **Seeds.** `SEED` sets ns-3's `--RngRun` and the recommender's seed. Two runs with the same
  settings and seed give identical results, which is useful for debugging.
- **Re-running.** `--run-plan` skips runs that already have a `summary.txt`. Delete
  `results/runs/<id>` to redo one. `--collect` overwrites the matching per-run CSVs in
  `results/sweep/`.
- **runs.csv** is split on commas by the sbatch script, so ns-3 args in it must not contain
  commas (`--plan` checks). Keep e.g. `--edgeGhz=4,1,1` out of the plan, or change the format.
- **Speed.** ns-3 and the recommender exchange one small message per task. Both sides set
  `TCP_NODELAY`; without it, Nagle + Linux delayed ACKs added ~40 ms of idle time per task.
- **Same results everywhere.** The image (optimized, Linux) and a default-profile laptop build
  (macOS) give the same decision and outcome for every task in all 185 runs, and the same
  summary.csv; only the last printed digit of a few timing columns can differ.
- **Tested:** `docker build` and the full plan through `--run-plan --docker` (Docker Desktop,
  linux/arm64 on an M2); `run-one.sh`, `--plan`/`--run-plan`/`--collect` natively; the sbatch
  script's line parsing and argument passing (with stand-in `module`/`apptainer` commands).
- **Not yet tested:** the GitHub workflow (linux/amd64 build and GHCR push), and anything on
  Aire (Apptainer, Slurm).
