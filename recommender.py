#!/usr/bin/env python3
"""Recommender: the 'outside process' that ns-3 talks to.

Protocol (one JSON object per line, over TCP on localhost):
  ns-3 -> here : {"type": "request", "task_id": 7, "user": 2, "t": 1.53, "size": 1000,
                  "work": 0.12, "deadline": 0.2, "queues": [3, 0, 1], "edge_ghz": [4, 1, 1]}
  here -> ns-3 : {"edge": 1}
  ns-3 -> here : {"type": "feedback", "task_id": 7, "user": 2, "edge": 1, "outcome": "met",
                  "met_deadline": true, "delay": 0.031, "net_delay": 0.006,
                  "queue_wait": 0.0, "edge_load": 2}
  (feedback gets no reply; every task gets exactly one feedback message)

outcome is one of: met, queue_full, evicted, expired, lost.

Policies (--policy):
  fixed         always the same edge (--fixed-edge), the week-8 dummy
  random        uniform random edge
  shortest_wait edge with the smallest (tasks queued + 1) / speed; uses live queue state
  learner       learns a success rate for each (user, edge) pair from feedback only,
                borrows other users' experience for edges a user hasn't tried,
                and explores at random with probability --epsilon

--ttl T makes any policy "time-bound": a user reuses its last recommendation for T
simulated seconds instead of asking again (0 = ask for every task).
"""
import argparse
import csv
import json
import random
import socket


# ---------------------------------------------------------------------------
# Policies: choose(request) -> edge index; feedback(msg) is called later.
# ---------------------------------------------------------------------------
class Fixed:
    def __init__(self, args, rng):
        self.edge = args.fixed_edge

    def choose(self, req):
        return min(self.edge, len(req["queues"]) - 1)

    def feedback(self, msg):
        pass


class Random:
    def __init__(self, args, rng):
        self.rng = rng

    def choose(self, req):
        return self.rng.randrange(len(req["queues"]))

    def feedback(self, msg):
        pass


class ShortestWait:
    """Estimated finish ~ (tasks already there + this one) / CPU speed."""

    def __init__(self, args, rng):
        self.rng = rng

    def choose(self, req):
        est = [(q + 1) / g for q, g in zip(req["queues"], req["edge_ghz"])]
        best = min(est)
        return self.rng.choice([e for e, v in enumerate(est) if v == best])

    def feedback(self, msg):
        pass


class Learner:
    """Collaborative-style recommender that sees only feedback, never queue state.

    rating[(user, edge)] is an exponential moving average of success (1 = met deadline),
    so recent outcomes count most and old experience fades (alpha controls how fast).
    """

    def __init__(self, args, rng):
        self.rng, self.eps, self.alpha = rng, args.epsilon, args.alpha
        self.rating = {}

    def score(self, user, edge):
        if (user, edge) in self.rating:
            return self.rating[(user, edge)]
        others = [r for (u, e), r in self.rating.items() if e == edge]
        return sum(others) / len(others) if others else 1.0  # optimistic: untried looks good

    def choose(self, req):
        n = len(req["queues"])
        if self.rng.random() < self.eps:
            return self.rng.randrange(n)
        scores = [self.score(req["user"], e) for e in range(n)]
        best = max(scores)
        return self.rng.choice([e for e, s in enumerate(scores) if s == best])

    def feedback(self, msg):
        key = (msg["user"], msg["edge"])
        y = 1.0 if msg["met_deadline"] else 0.0
        old = self.rating.get(key)
        self.rating[key] = y if old is None else (1 - self.alpha) * old + self.alpha * y


POLICIES = {"fixed": Fixed, "random": Random, "shortest_wait": ShortestWait, "learner": Learner}


class TimeBound:
    """Wraps a policy: reuse each user's last answer for `ttl` simulated seconds."""

    def __init__(self, inner, ttl):
        self.inner, self.ttl, self.cache = inner, ttl, {}

    def choose(self, req):
        hit = self.cache.get(req["user"])
        if hit and req["t"] - hit[0] < self.ttl:
            return hit[1]
        edge = self.inner.choose(req)
        self.cache[req["user"]] = (req["t"], edge)
        return edge

    def feedback(self, msg):
        self.inner.feedback(msg)


# ---------------------------------------------------------------------------
FIELDS = ["policy", "ttl", "seed", "task_id", "user", "t", "work", "deadline", "queues",
          "edge", "outcome", "met_deadline", "delay", "net_delay", "queue_wait", "edge_load"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--log", default="feedback.csv")
    p.add_argument("--policy", choices=POLICIES, default="fixed")
    p.add_argument("--fixed-edge", type=int, default=1)
    p.add_argument("--epsilon", type=float, default=0.1)
    p.add_argument("--alpha", type=float, default=0.2)
    p.add_argument("--ttl", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()

    rng = random.Random(args.seed)
    policy = POLICIES[args.policy](args, rng)
    if args.ttl > 0:
        policy = TimeBound(policy, args.ttl)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    print(f"[recommender] policy={args.policy} ttl={args.ttl} listening on "
          f"127.0.0.1:{args.port}", flush=True)

    conn, _ = srv.accept()
    # Reply immediately to each small message (no Nagle batching; see offload-edge.cc).
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    counts = {}
    with conn, conn.makefile("rw") as f, open(args.log, "w", newline="") as out:
        w = csv.DictWriter(out, fieldnames=FIELDS)
        w.writeheader()
        requests = {}
        for line in f:
            msg = json.loads(line)
            if msg["type"] == "request":
                edge = policy.choose(msg)
                msg["edge"] = edge
                requests[msg["task_id"]] = msg
                f.write(json.dumps({"edge": edge}) + "\n")
                f.flush()
            elif msg["type"] == "feedback":
                policy.feedback(msg)
                req = requests.pop(msg["task_id"])
                counts[msg["outcome"]] = counts.get(msg["outcome"], 0) + 1
                w.writerow(dict(
                    policy=args.policy, ttl=args.ttl, seed=args.seed,
                    task_id=msg["task_id"], user=req["user"], t=req["t"], work=req["work"],
                    deadline=req["deadline"], queues=" ".join(map(str, req["queues"])),
                    edge=msg["edge"], outcome=msg["outcome"],
                    met_deadline=msg["met_deadline"], delay=msg["delay"],
                    net_delay=msg["net_delay"], queue_wait=msg["queue_wait"],
                    edge_load=msg["edge_load"]))
    total = sum(counts.values())
    met = counts.get("met", 0)
    print(f"[recommender] done: {total} tasks, met deadline {met} "
          f"({met / max(total, 1):.1%}), outcomes {counts}, unanswered {len(requests)}",
          flush=True)


if __name__ == "__main__":
    main()
