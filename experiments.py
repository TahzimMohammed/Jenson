#!/usr/bin/env python3
"""Run a policy comparison sweep through run.sh, then summarise it.

  python3 experiments.py                  # full sweep, 4 runs at a time
  python3 experiments.py --jobs 8 --seeds 10
  python3 experiments.py --analyse-only   # just rebuild the table and charts

Same runs, one isolated folder each (for Slurm, see docs/BIG-RUNS.md):
  python3 experiments.py --plan           # write runs.csv: one line per run
  python3 experiments.py --run-plan --jobs 4 [--docker IMAGE]   # run runs.csv here
  sbatch --array=1-<N>%50 slurm/sweep.sbatch                    # ...or on the cluster
  python3 experiments.py --collect        # results/runs/* -> summary.csv + charts

Sweep 1 (policies): every policy x load level x seed.
Sweep 2 (time-bound): shortest_wait with different recommendation lifetimes (TTL)
at one load level, to show how stale recommendations hurt.
Sweep 3 (TTL trade-off): shortest_wait and learner, TTL x decision delay at one load
level. Each recommender query costs --decisionDelay simulated seconds; a cached (TTL)
answer is free. So a TTL > 0 trades staleness for fewer slow queries.

Outputs in results/sweep/: one CSV per run, summary.csv, policies.png, ttl.png,
ttl_delay.png.
Uses only the standard library plus matplotlib (for the charts).
"""
import argparse
import csv
import glob
import math
import os
import shutil
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "results", "sweep")

POLICIES = ["fixed", "random", "fastest", "learner", "shortest_wait"]
# mean seconds between tasks per user -> offered load. With 10 users, the default
# edges (4,1,1 GHz) and 0.1 Gcycles/task, total capacity is ~60 tasks/s.
INTERVALS = [0.5, 0.3, 0.2]
TTLS = [0, 0.25, 1, 4]
TTL_INTERVAL = 0.3
DELAYS = [0, 0.02, 0.05]                    # sweep 3: simulated seconds per query
DELAY_POLICIES = ["shortest_wait", "learner"]
SCENARIO = ["--nUsers=10", "--simTime=30"]
RUNS_CSV = os.path.join(HERE, "runs.csv")
RUNS_DIR = os.path.join(HERE, "results", "runs")


def run_name(policy, ttl, dd, interval, seed):
    """File name for one run. dd (decision delay) is only in the name when > 0."""
    dd_part = f"_dd{dd}" if dd else ""
    return f"{policy}_ttl{ttl}{dd_part}_int{interval}_seed{seed}"


def parse_name(name):
    policy, rest = name.split("_ttl")
    ttl, rest = rest.split("_int")
    ttl, dd = ttl.split("_dd") if "_dd" in ttl else (ttl, 0)
    interval, seed = rest.split("_seed")
    return policy, float(ttl), float(dd), float(interval), int(seed)


def all_jobs(seeds):
    """Every run of sweeps 1-3 as (policy, ttl, decision_delay, interval, seed), no repeats."""
    jobs = [(p, 0, 0, i, s) for p in POLICIES for i in INTERVALS for s in seeds]
    jobs += [("shortest_wait", t, 0, TTL_INTERVAL, s) for t in TTLS if t > 0 for s in seeds]
    jobs += [(p, t, d, TTL_INTERVAL, s) for p in DELAY_POLICIES for t in TTLS
             for d in DELAYS for s in seeds]
    return list(dict.fromkeys(jobs))


def run_one(job):
    policy, ttl, dd, interval, seed, port = job
    out = os.path.join(OUTDIR, run_name(policy, ttl, dd, interval, seed) + ".csv")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    env = dict(os.environ, POLICY=policy, TTL=str(ttl), SEED=str(seed), PORT=str(port),
               OUT=out, SKIP_BUILD="1")
    cmd = [os.path.join(HERE, "run.sh"), *scenario_args(dd, interval)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"FAILED {job}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}\n")
        if os.path.exists(out):
            os.remove(out)  # don't keep a partial file
        return None
    return out


def scenario_args(dd, interval):
    """ns-3 arguments for one run (the same for run.sh and run-one.sh)."""
    return [f"--meanInterval={interval}", *SCENARIO] + ([f"--decisionDelay={dd}"] if dd else [])


def plan(seeds):
    """Write runs.csv (run_id,policy,ttl,seed,args): the run list for Slurm or --run-plan."""
    with open(RUNS_CSV, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["run_id", "policy", "ttl", "seed", "args"])
        jobs = all_jobs(seeds)
        for n, (policy, ttl, dd, interval, seed) in enumerate(jobs, 1):
            args = " ".join(scenario_args(dd, interval))
            assert "," not in args, "slurm/sweep.sbatch splits runs.csv on commas"
            w.writerow([f"{n:04d}", policy, ttl, seed, args])
    print(f"{len(jobs)} runs -> {RUNS_CSV}")
    print(f"cluster: sbatch --array=1-{len(jobs)}%50 slurm/sweep.sbatch")


def read_plan():
    if not os.path.exists(RUNS_CSV):
        sys.exit("runs.csv not found: run python3 experiments.py --plan first")
    with open(RUNS_CSV) as f:
        return list(csv.DictReader(f))


def run_plan(jobs, docker_image):
    """Run every line of runs.csv through run-one.sh, like the Slurm array job does."""
    runs = read_plan()
    os.makedirs(RUNS_DIR, exist_ok=True)
    if not docker_image:
        build()  # run-one.sh uses the compiled binary, so make sure it matches scratch/

    def one(run):
        out = os.path.join(RUNS_DIR, run["run_id"])
        if os.path.exists(os.path.join(out, "summary.txt")) and os.path.getsize(
                os.path.join(out, "summary.txt")):
            return run["run_id"], "skipped (done)"
        env = dict(POLICY=run["policy"], TTL=run["ttl"], SEED=run["seed"])
        if docker_image:
            cmd = ["docker", "run", "--rm", "-v", f"{RUNS_DIR}:/results"]
            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd += [docker_image, "run-one.sh", f"/results/{run['run_id']}", *run["args"].split()]
            r = subprocess.run(cmd, capture_output=True, text=True)
        else:
            cmd = [os.path.join(HERE, "run-one.sh"), out, *run["args"].split()]
            r = subprocess.run(cmd, env=dict(os.environ, **env), capture_output=True, text=True)
        return run["run_id"], "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()[-300:]}"

    failed = 0
    with ThreadPoolExecutor(jobs) as pool:
        for run_id, status in pool.map(one, runs):
            failed += status.startswith("FAILED")
            if status != "ok":
                print(run_id, status, flush=True)
    print(f"{len(runs)} runs, {failed} failed")


def collect():
    """Copy each finished results/runs/<id>/feedback.csv into results/sweep/ under its
    usual name, then build summary.csv and the charts exactly as for a local sweep."""
    done = missing = 0
    for run in read_plan():
        src = os.path.join(RUNS_DIR, run["run_id"])
        fb, summ = os.path.join(src, "feedback.csv"), os.path.join(src, "summary.txt")
        if not (os.path.exists(summ) and os.path.getsize(summ) and os.path.exists(fb)):
            missing += 1
            continue
        params = dict(a.lstrip("-").split("=", 1) for a in run["args"].split())
        # raw strings from runs.csv, so names match a local sweep exactly (ttl1, not ttl1.0)
        name = run_name(run["policy"], run["ttl"], params.get("decisionDelay", ""),
                        params["meanInterval"], run["seed"])
        shutil.copyfile(fb, os.path.join(OUTDIR, name + ".csv"))
        done += 1
    print(f"collected {done} runs from {RUNS_DIR}" + (f", {missing} missing/unfinished" if missing else ""))
    if not done:
        sys.exit("nothing to collect")


def build():
    ns3 = os.environ.get("NS3_DIR", os.path.expanduser("~/ns-3-dev"))
    subprocess.run(f'cp "{HERE}"/scratch/*.cc "{ns3}/scratch/"', shell=True, check=True)
    subprocess.run(["./ns3", "build", "offload-edge"], cwd=ns3, check=True)


def mean_ci(xs):
    m = statistics.mean(xs)
    ci = 1.96 * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0
    return m, ci


def analyse():
    rows = []
    for path in sorted(glob.glob(os.path.join(OUTDIR, "*_seed*.csv"))):
        policy, ttl, dd, interval, seed = parse_name(os.path.basename(path)[:-4])
        with open(path) as f:
            tasks = list(csv.DictReader(f))
        if not tasks:
            continue
        met = [t for t in tasks if t["met_deadline"] == "True"]
        outcomes = {}
        for t in tasks:
            outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1
        rows.append(dict(
            policy=policy, ttl=ttl, dd=dd, interval=interval, seed=seed,
            tasks=len(tasks), hit=len(met) / len(tasks),
            delay=statistics.mean(float(t["delay"]) for t in met) if met else float("nan"),
            **{f"frac_{k}": v / len(tasks) for k, v in outcomes.items()}))

    groups = {}
    for r in rows:
        groups.setdefault((r["policy"], r["ttl"], r["dd"], r["interval"]), []).append(r)
    summary = []
    for (policy, ttl, dd, interval), rs in sorted(groups.items()):
        hit, hit_ci = mean_ci([r["hit"] for r in rs])
        delays = [r["delay"] for r in rs if not math.isnan(r["delay"])]
        delay, delay_ci = mean_ci(delays) if delays else (float("nan"), 0.0)
        s = dict(policy=policy, ttl=ttl, decision_delay=dd, interval=interval,
                 load_tasks_per_s=round(10 / interval, 1), seeds=len(rs),
                 hit_rate=round(hit, 3), hit_ci95=round(hit_ci, 3),
                 delay_ms=round(1000 * delay, 1), delay_ci95_ms=round(1000 * delay_ci, 1))
        for k in ["met", "queue_full", "evicted", "expired", "lost"]:
            s[k] = round(statistics.mean(r.get(f"frac_{k}", 0) for r in rs), 3)
        summary.append(s)

    with open(os.path.join(OUTDIR, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)

    print(f"{'policy':14}{'ttl':>5}{'dd':>6}{'tasks/s':>9}{'hit rate':>16}{'delay ms':>10}"
          f"{'queue_full':>12}{'expired':>9}")
    for s in summary:
        print(f"{s['policy']:14}{s['ttl']:>5}{s['decision_delay']:>6}{s['load_tasks_per_s']:>9}"
              f"{s['hit_rate']:>9.3f} ±{s['hit_ci95']:.3f}{s['delay_ms']:>10}"
              f"{s['queue_full']:>12}{s['expired']:>9}")
    plot(summary)


def plot(summary):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping charts (summary.csv is written)")
        return

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for policy in POLICIES:
        pts = sorted((s["load_tasks_per_s"], s["hit_rate"], s["hit_ci95"])
                     for s in summary
                     if s["policy"] == policy and s["ttl"] == 0 and s["decision_delay"] == 0)
        if pts:
            x, y, e = zip(*pts)
            ax.errorbar(x, y, yerr=e, marker="o", capsize=3, label=policy)
    ax.set(xlabel="Offered load (tasks per second, all users)",
           ylabel="Fraction of tasks meeting deadline", ylim=(0, 1.05),
           title="Edge selection policies in ns-3 (capacity ~60 tasks/s)")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "policies.png"), dpi=150)

    pts = sorted((s["ttl"], s["hit_rate"], s["hit_ci95"]) for s in summary
                 if s["policy"] == "shortest_wait" and s["interval"] == TTL_INTERVAL
                 and s["decision_delay"] == 0)
    if len(pts) > 1:
        fig, ax = plt.subplots(figsize=(6, 4))
        x, y, e = zip(*pts)
        ax.errorbar(range(len(x)), y, yerr=e, marker="o", capsize=3)
        ax.set_xticks(range(len(x)), [f"{t:g}" for t in x])
        ax.set(xlabel="Recommendation lifetime, TTL (s); 0 = ask for every task",
               ylabel="Fraction of tasks meeting deadline (zoomed)",
               ylim=(max(0, min(y) - max(e) - 0.05), 1.0),
               title=f"Time-bound recommendations (shortest_wait, {10 / TTL_INTERVAL:.0f} tasks/s)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(OUTDIR, "ttl.png"), dpi=150)

    # Sweep 3: does a TTL pay off once each query costs time?
    fig, axes = plt.subplots(1, len(DELAY_POLICIES), figsize=(11, 4), sharey=True)
    plotted = False
    for ax, policy in zip(axes, DELAY_POLICIES):
        for dd in DELAYS:
            pts = sorted((s["ttl"], s["hit_rate"], s["hit_ci95"]) for s in summary
                         if s["policy"] == policy and s["interval"] == TTL_INTERVAL
                         and s["decision_delay"] == dd)
            if len(pts) > 1:
                x, y, e = zip(*pts)
                ax.errorbar(range(len(x)), y, yerr=e, marker="o", capsize=3,
                            label=f"{1000 * dd:g} ms per query")
                ax.set_xticks(range(len(x)), [f"{t:g}" for t in x])
                plotted = True
        ax.set(title=policy, xlabel="TTL (s); 0 = ask for every task")
        ax.grid(alpha=0.3)
        ax.legend()
    axes[0].set_ylabel("Fraction of tasks meeting deadline")
    fig.suptitle(f"TTL vs decision delay ({10 / TTL_INTERVAL:.0f} tasks/s)")
    fig.tight_layout()
    if plotted:
        fig.savefig(os.path.join(OUTDIR, "ttl_delay.png"), dpi=150)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--analyse-only", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="write runs.csv and stop")
    mode.add_argument("--run-plan", action="store_true",
                      help="run runs.csv here via run-one.sh (one folder per run)")
    mode.add_argument("--collect", action="store_true",
                      help="results/runs/* -> summary.csv + charts")
    ap.add_argument("--docker", metavar="IMAGE", help="with --run-plan: run inside this image")
    args = ap.parse_args()
    os.makedirs(OUTDIR, exist_ok=True)

    if args.plan:
        plan(range(1, args.seeds + 1))
        return
    if args.run_plan:
        run_plan(args.jobs, args.docker)
        return
    if args.collect:
        collect()
    elif not args.analyse_only:
        build()
        jobs = all_jobs(range(1, args.seeds + 1))
        jobs = [(*j, 6000 + n) for n, j in enumerate(jobs)]  # unique port per run
        done = 0
        with ThreadPoolExecutor(args.jobs) as pool:
            for _ in pool.map(run_one, jobs):
                done += 1
                print(f"\r{done}/{len(jobs)} runs", end="", flush=True)
        print()
    analyse()


if __name__ == "__main__":
    main()
