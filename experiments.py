#!/usr/bin/env python3
"""Run a policy comparison sweep through run.sh, then summarise it.

  python3 experiments.py                  # full sweep, 4 runs at a time
  python3 experiments.py --jobs 8 --seeds 10
  python3 experiments.py --analyse-only   # just rebuild the table and charts

Sweep 1 (policies): every policy x load level x seed.
Sweep 2 (time-bound): shortest_wait with different recommendation lifetimes (TTL)
at one load level, to show how stale recommendations hurt.

Outputs in results/sweep/: one CSV per run, summary.csv, policies.png, ttl.png.
Uses only the standard library plus matplotlib (for the charts).
"""
import argparse
import csv
import glob
import math
import os
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "results", "sweep")

POLICIES = ["fixed", "random", "learner", "shortest_wait"]
# mean seconds between tasks per user -> offered load. With 10 users, the default
# edges (4,1,1 GHz) and 0.1 Gcycles/task, total capacity is ~60 tasks/s.
INTERVALS = [0.5, 0.3, 0.2]
TTLS = [0, 0.25, 1, 4]
TTL_INTERVAL = 0.3
SCENARIO = ["--nUsers=10", "--simTime=30"]


def run_one(job):
    policy, ttl, interval, seed, port = job
    out = os.path.join(OUTDIR, f"{policy}_ttl{ttl}_int{interval}_seed{seed}.csv")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    env = dict(os.environ, POLICY=policy, TTL=str(ttl), SEED=str(seed), PORT=str(port),
               OUT=out, SKIP_BUILD="1")
    cmd = [os.path.join(HERE, "run.sh"), f"--meanInterval={interval}", *SCENARIO]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"FAILED {job}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}\n")
        if os.path.exists(out):
            os.remove(out)  # don't keep a partial file
        return None
    return out


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
        name = os.path.basename(path)[:-4]
        policy, rest = name.split("_ttl")
        ttl, rest = rest.split("_int")
        interval, seed = rest.split("_seed")
        with open(path) as f:
            tasks = list(csv.DictReader(f))
        if not tasks:
            continue
        met = [t for t in tasks if t["met_deadline"] == "True"]
        outcomes = {}
        for t in tasks:
            outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1
        rows.append(dict(
            policy=policy, ttl=float(ttl), interval=float(interval), seed=int(seed),
            tasks=len(tasks), hit=len(met) / len(tasks),
            delay=statistics.mean(float(t["delay"]) for t in met) if met else float("nan"),
            **{f"frac_{k}": v / len(tasks) for k, v in outcomes.items()}))

    groups = {}
    for r in rows:
        groups.setdefault((r["policy"], r["ttl"], r["interval"]), []).append(r)
    summary = []
    for (policy, ttl, interval), rs in sorted(groups.items()):
        hit, hit_ci = mean_ci([r["hit"] for r in rs])
        delays = [r["delay"] for r in rs if not math.isnan(r["delay"])]
        delay, delay_ci = mean_ci(delays) if delays else (float("nan"), 0.0)
        s = dict(policy=policy, ttl=ttl, interval=interval,
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

    print(f"{'policy':14}{'ttl':>5}{'tasks/s':>9}{'hit rate':>16}{'delay ms':>10}"
          f"{'queue_full':>12}{'expired':>9}")
    for s in summary:
        print(f"{s['policy']:14}{s['ttl']:>5}{s['load_tasks_per_s']:>9}"
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
                     for s in summary if s["policy"] == policy and s["ttl"] == 0)
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
                 if s["policy"] == "shortest_wait" and s["interval"] == TTL_INTERVAL)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--analyse-only", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUTDIR, exist_ok=True)

    if not args.analyse_only:
        build()
        jobs = [(p, 0, i, s) for p in POLICIES for i in INTERVALS
                for s in range(1, args.seeds + 1)]
        jobs += [("shortest_wait", t, TTL_INTERVAL, s) for t in TTLS if t > 0
                 for s in range(1, args.seeds + 1)]
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
