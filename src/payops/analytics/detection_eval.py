"""Measure how well the detection baseline finds injected incidents.

Run:  python -m payops.analytics.detection_eval --seeds 42 1 2 3 4 5 6 7

For each seed it generates a fresh 90-day dataset in memory (nothing is
written or loaded), runs detection, and compares the result with that seed's
ground truth.

* A detected window is a TRUE alarm if it is on the right provider and
  overlaps an injected incident in time; otherwise it is a FALSE alarm.
* precision = true alarms / all alarms        ("when it raises an alarm, how often is it right?")
* recall    = incidents found / incidents injected  ("how many real problems does it catch?")

This is evaluation code: it is the only place, besides tests, that may use ground truth.
"""

from __future__ import annotations

import argparse

import pandas as pd

from payops.analytics.detection import detect_anomalies
from payops.data.generator import generate
from payops.data.scenario import GenerationConfig


def evaluate_seed(seed: int) -> dict:
    data = generate(GenerationConfig(seed=seed))
    t = data.transactions
    t = t.assign(failed=(t["status"] == "FAILED").astype(int), hour=t["created_at"].dt.floor("h"))
    hourly = t.groupby(["provider_code", "hour"]).agg(attempts=("failed", "size"), failures=("failed", "sum")).reset_index()
    windows = detect_anomalies(hourly)
    truth = data.ground_truth["injected_incidents"]

    def overlaps(w, inc) -> bool:
        return (w.segment == inc["provider_code"] and w.start < pd.Timestamp(inc["actual_end"])
                and w.end > pd.Timestamp(inc["actual_start"]))

    true_alarms = sum(any(overlaps(w, i) for i in truth) for w in windows)
    found = {i["key"]: any(overlaps(w, i) for w in windows) for i in truth}
    return {"seed": seed, "alarms": len(windows), "true_alarms": true_alarms,
            "false_alarms": len(windows) - true_alarms, "found": found}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate anomaly detection against injected incidents.")
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 1, 2, 3, 4, 5, 6, 7])
    args = parser.parse_args(argv)

    results = [evaluate_seed(s) for s in args.seeds]
    for r in results:
        found = ", ".join(f"{k}={'found' if v else 'MISSED'}" for k, v in r["found"].items())
        print(f"seed {r['seed']:>4}: alarms {r['alarms']}, false alarms {r['false_alarms']}, {found}")
    alarms = sum(r["alarms"] for r in results)
    true_alarms = sum(r["true_alarms"] for r in results)
    incidents = sum(len(r["found"]) for r in results)
    caught = sum(sum(r["found"].values()) for r in results)
    print(f"\nprecision {true_alarms}/{alarms} = {true_alarms / alarms:.1%}" if alarms else "\nprecision: no alarms")
    print(f"recall    {caught}/{incidents} = {caught / incidents:.1%}")
    for key in sorted({k for r in results for k in r["found"]}):
        hits = sum(r["found"][key] for r in results)
        print(f"  {key:<10} found in {hits}/{len(results)} seeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
