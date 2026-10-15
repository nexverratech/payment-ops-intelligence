"""Generates the synthetic payment dataset.

Run:  python -m payops.data.generator --seed 42 --out data/generated

How the simulation works, in plain words:
1. For every hour of the window, decide how many payments happen
   (busier in the evening, a bit busier on Saturdays, slowly growing).
2. Give each payment a provider, method, customer and amount.
3. Decide whether it fails, using that provider's normal failure rate
   plus a little day-to-day wobble.
4. Inject the planned incidents: during each one, payments at the affected
   provider get an EXTRA chance of failing with a specific reason.
5. Write CSV files, a manifest (seed, parameters, file fingerprints) and a
   separate ground_truth.json that only the evaluation step may read.

Everything random comes from one seeded generator, so the same seed and
parameters always give the same files on the same library versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from payops.data.scenario import GenerationConfig, InjectedIncident

DATASET_FORMAT_VERSION = 1

_DEPLOY_SERVICES = {
    # service: (owning team, starting version, example change summaries)
    "checkout-service": ("payments-web", (2, 4), ["UI copy update", "retry header tweak", "checkout page refactor"]),
    "risk-engine": ("risk", (3, 1), ["model threshold update", "rule cache tuning"]),
    "ledger-service": ("finance-platform", (1, 9), ["reconciliation job fix", "index maintenance"]),
    "notification-service": ("comms", (4, 0), ["email template update", "SMS provider config"]),
    "fraud-rules": ("risk", (5, 0), ["velocity rule adjustment", "new blocklist entries"]),
}


@dataclass
class GeneratedData:
    providers: pd.DataFrame
    provider_incidents: pd.DataFrame
    deployments: pd.DataFrame
    transactions: pd.DataFrame
    ground_truth: dict


def generate(config: GenerationConfig | None = None) -> GeneratedData:
    cfg = config or GenerationConfig()
    rng = np.random.default_rng(cfg.seed)
    window_start = datetime(cfg.start_date.year, cfg.start_date.month, cfg.start_date.day, tzinfo=timezone.utc)
    n_hours = cfg.days * 24

    # ---- 1. How many payments in each hour ---------------------------------
    hour_index = np.arange(n_hours)
    day_index = hour_index // 24
    weekday = (window_start.weekday() + day_index) % 7
    hourly = np.array(cfg.hourly_weights)
    expected = (
        cfg.base_daily_volume
        * (1 + cfg.daily_growth * day_index)
        * np.array(cfg.weekday_multipliers)[weekday]
        * hourly[hour_index % 24] / hourly.sum()
    )
    counts = rng.poisson(expected)
    n = int(counts.sum())

    # Seconds since window start, sorted so transaction IDs follow time order.
    seconds = np.sort(np.repeat(hour_index, counts) * 3600 + rng.integers(0, 3600, n))
    txn_day = seconds // 86_400

    # ---- 2. Who, how, how much --------------------------------------------
    provider_codes = np.array([p.code for p in cfg.providers])
    provider_idx = rng.choice(len(cfg.providers), size=n, p=_normalised([p.traffic_share for p in cfg.providers]))
    method_names = np.array([m[0] for m in cfg.payment_methods])
    method_idx = rng.choice(len(cfg.payment_methods), size=n, p=_normalised([m[1] for m in cfg.payment_methods]))
    customer_num = rng.integers(1, cfg.customers + 1, size=n)
    amount_multiplier = np.array([m[2] for m in cfg.payment_methods])[method_idx]
    amounts = np.round(
        np.clip(np.exp(rng.normal(np.log(cfg.median_amount), cfg.amount_spread, n)) * amount_multiplier, 1.0, 25_000.0),
        2,
    )

    # ---- 3. Normal failures ------------------------------------------------
    base_rate = np.array([p.base_failure_rate for p in cfg.providers])
    daily_wobble = np.exp(rng.normal(0.0, cfg.daily_failure_noise, size=(len(cfg.providers), cfg.days)))
    fail_prob = base_rate[provider_idx] * daily_wobble[provider_idx, txn_day]
    failed = rng.random(n) < fail_prob
    reason_names = np.array([r[0] for r in cfg.failure_reasons])
    reason = np.where(
        failed,
        reason_names[rng.choice(len(reason_names), size=n, p=_normalised([r[1] for r in cfg.failure_reasons]))],
        None,
    ).astype(object)

    # Latency: typical time per provider, with natural variation.
    median_latency = np.array([p.median_latency_ms for p in cfg.providers])
    latency = median_latency[provider_idx] * np.exp(rng.normal(0.0, 0.35, n))

    # ---- 4. Inject incidents -----------------------------------------------
    incidents_rows, gt_incidents, event_windows = [], [], []
    for inc in cfg.incidents:
        start_s = ((cfg.days - 1 - inc.start_days_before_end) * 24 + inc.start_hour) * 3600
        end_s = start_s + int(inc.duration_hours * 3600)
        intensity = _incident_intensity(seconds, start_s, end_s)
        affected = (provider_codes[provider_idx] == inc.provider_code) & (intensity > 0)

        extra_fail = affected & ~failed & (rng.random(n) < inc.peak_extra_failure_rate * intensity)
        failed |= extra_fail
        reason[extra_fail] = inc.failure_reason
        latency[affected] *= 1 + (inc.latency_multiplier_at_peak - 1) * intensity[affected]

        gt = {
            "key": inc.key,
            "provider_code": inc.provider_code,
            "failure_reason": inc.failure_reason,
            "actual_start": _iso(window_start, start_s),
            "actual_end": _iso(window_start, end_s),
            "peak_extra_failure_rate": inc.peak_extra_failure_rate,
            "injected_failures": int(extra_fail.sum()),
        }
        if inc.reported_delay_hours is not None:
            rep_start = start_s + int(inc.reported_delay_hours * 3600)
            rep_end = rep_start + int((inc.reported_duration_hours or inc.duration_hours) * 3600)
            incidents_rows.append(_incident_row(inc, window_start, rep_start, rep_end))
            event_windows.append((inc, rep_start, rep_end, gt))
        gt_incidents.append(gt)

    # A provider notice with NO real effect (scheduled maintenance): a negative example.
    maint_start = (40 * 24 + 2) * 3600 if cfg.days > 41 else 2 * 3600
    incidents_rows.append({
        "provider_code": "BLUERIVER",
        "reported_start_at": _iso(window_start, maint_start),
        "reported_end_at": _iso(window_start, maint_start + 3600),
        "severity": "info",
        "title": "BlueRiver Payments: scheduled maintenance",
        "description": "Planned maintenance window. No customer impact expected.",
        "_key": "maintenance",
    })

    incidents = pd.DataFrame(incidents_rows).sort_values("reported_start_at", kind="stable").reset_index(drop=True)
    incidents.insert(0, "incident_id", [f"INC-{i:04d}" for i in range(1, len(incidents) + 1)])
    key_to_id = dict(zip(incidents["_key"], incidents["incident_id"]))
    incidents = incidents.drop(columns="_key")

    # Failed payments that happened while a provider notice was live carry its reference,
    # as a real provider would return it. Payments before the notice was posted do not.
    event_id = np.full(n, None, dtype=object)
    for inc, rep_start, rep_end, gt in event_windows:
        tagged = (
            failed
            & (provider_codes[provider_idx] == inc.provider_code)
            & (reason == inc.failure_reason)
            & (seconds >= rep_start)
            & (seconds < rep_end)
        )
        event_id[tagged] = key_to_id[inc.key]
        gt["provider_incident_id"] = key_to_id[inc.key]
        gt["provider_reported_start"] = _iso(window_start, rep_start)
        gt["provider_reported_end"] = _iso(window_start, rep_end)

    # Final latency rules: timeouts waited the full timeout; other declines are quick.
    timed_out = failed & (reason == "TIMEOUT")
    latency = np.where(failed & ~timed_out, latency * 0.6, latency)
    latency = np.where(timed_out, cfg.timeout_latency_ms + rng.integers(0, 500, n), latency)

    transactions = pd.DataFrame({
        "transaction_id": np.char.add("TXN-", np.char.zfill(np.arange(1, n + 1).astype(str), 8)),
        "created_at": pd.to_datetime(window_start) + pd.to_timedelta(seconds, unit="s"),
        "customer_id": np.char.add("CUST-", np.char.zfill(customer_num.astype(str), 6)),
        "provider_code": provider_codes[provider_idx],
        "payment_method": method_names[method_idx],
        "amount": amounts,
        "currency": cfg.currency,
        "status": np.where(failed, "FAILED", "SUCCEEDED"),
        "failure_reason": reason,
        "latency_ms": np.round(latency).astype(np.int64),
        "provider_event_id": event_id,
    })

    deployments, deploy_truth = _deployments(cfg, rng, window_start, gt_incidents)

    providers = pd.DataFrame(
        [{"provider_code": p.code, "display_name": p.display_name} for p in cfg.providers]
    )

    ground_truth = {
        "about": (
            "Answer key for evaluation only. It must never be loaded into the database "
            "or shown to the agents."
        ),
        "injected_incidents": gt_incidents,
        "no_effect_provider_notices": [key_to_id["maintenance"]],
        "deployments": deploy_truth,
        "causal_truth_in_simulation": (
            "Failures were generated from the provider alone. No deployment changes any "
            "failure rate in this simulation. A correct report treats the deployment that "
            "precedes the primary incident as a correlated event, not a proven cause."
        ),
    }
    return GeneratedData(providers, incidents, deployments, transactions, ground_truth)


def _incident_intensity(seconds: np.ndarray, start_s: int, end_s: int) -> np.ndarray:
    """0 outside the incident; ramps up, holds at 1, then ramps down."""
    duration = end_s - start_s
    ramp_up = min(3600, duration / 4)
    ramp_down = min(5400, duration / 4)
    rel = seconds - start_s
    inside = (rel >= 0) & (rel < duration)
    level = np.minimum.reduce([np.ones_like(rel, dtype=float), rel / ramp_up, (duration - rel) / ramp_down])
    return np.where(inside, np.clip(level, 0.0, 1.0), 0.0)


def _incident_row(inc: InjectedIncident, window_start: datetime, rep_start: int, rep_end: int) -> dict:
    return {
        "provider_code": inc.provider_code,
        "reported_start_at": _iso(window_start, rep_start),
        "reported_end_at": _iso(window_start, rep_end),
        "severity": inc.reported_severity,
        "title": inc.reported_title,
        "description": "Provider status page notice (synthetic).",
        "_key": inc.key,
    }


def _deployments(cfg: GenerationConfig, rng: np.random.Generator, window_start: datetime, gt_incidents: list[dict]):
    """Routine deployments (about two a week) plus the two scenario deployments."""
    quiet_zones = []  # keep routine deploys away from injected incidents, so the answer key stays clean
    for gt in gt_incidents:
        quiet_zones.append((
            _seconds_since(window_start, gt["actual_start"]) - 12 * 3600,
            _seconds_since(window_start, gt["actual_end"]) + 12 * 3600,
        ))

    services = list(_DEPLOY_SERVICES)
    rows = []
    for week_start_day in range(0, cfg.days, 7):
        for _ in range(2):
            day = week_start_day + int(rng.integers(0, 7))
            if day >= cfg.days:
                continue
            s = (day * 24 + int(rng.integers(9, 18))) * 3600 + int(rng.integers(0, 3600))
            if any(lo <= s <= hi for lo, hi in quiet_zones):
                continue
            rows.append({"service": services[int(rng.integers(0, len(services)))], "_s": s, "_key": None})

    primary = next((g for g in gt_incidents if g["key"] == "primary"), None)
    if primary is not None:
        p_start = _seconds_since(window_start, primary["actual_start"])
        rows.append({"service": "checkout-service", "_s": p_start - 2 * 3600, "_key": "temporally_correlated"})
        rows.append({"service": "fraud-rules", "_s": p_start - 2 * 86_400 - 4 * 3600, "_key": "red_herring"})

    rows.sort(key=lambda r: r["_s"])
    next_minor = {svc: v[1] for svc, (_, v, _) in _DEPLOY_SERVICES.items()}
    out, truth = [], {}
    for i, r in enumerate(rows, start=1):
        team, (major, _), summaries = _DEPLOY_SERVICES[r["service"]]
        next_minor[r["service"]] += 1
        dep_id = f"DEP-{i:04d}"
        out.append({
            "deployment_id": dep_id,
            "service": r["service"],
            "version": f"v{major}.{next_minor[r['service']]}",
            "deployed_at": _iso(window_start, r["_s"]),
            "environment": "production",
            "team": team,
            "change_summary": summaries[int(rng.integers(0, len(summaries)))],
        })
        if r["_key"]:
            truth[r["_key"]] = dep_id
    return pd.DataFrame(out), truth


# ---------------------------------------------------------------------------
# Writing files
# ---------------------------------------------------------------------------

FILE_ORDER = ("providers", "provider_incidents", "deployments", "transactions")


def write_dataset(data: GeneratedData, config: GenerationConfig, out_dir: Path) -> dict:
    """Write the CSVs, manifest.json and ground_truth.json. Returns the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in FILE_ORDER:
        frame: pd.DataFrame = getattr(data, name)
        path = out_dir / f"{name}.csv"
        # Fixed formats and "\n" line endings make the files byte-identical on Windows and Linux.
        frame.to_csv(path, index=False, lineterminator="\n", float_format="%.2f", date_format="%Y-%m-%dT%H:%M:%SZ")
        files[path.name] = {"rows": len(frame), "sha256": _sha256(path)}

    dataset_id = hashlib.sha256("".join(f["sha256"] for f in files.values()).encode()).hexdigest()[:16]
    manifest = {
        "dataset_id": dataset_id,
        "format_version": DATASET_FORMAT_VERSION,
        "seed": config.seed,
        "window_start": _iso(_start(config), 0),
        "window_end": _iso(_start(config), config.days * 86_400),
        "library_versions": {"numpy": np.__version__, "pandas": pd.__version__},
        # The incident definitions are the answer key, so they are kept OUT of the
        # manifest (which is loaded into the database) and saved in ground_truth.json.
        "parameters": {k: v for k, v in config.to_dict().items() if k != "incidents"},
        "files": files,
    }
    _write_json(out_dir / "manifest.json", manifest)
    _write_json(
        out_dir / "ground_truth.json",
        {"dataset_id": dataset_id, **data.ground_truth, "incident_definitions": config.to_dict()["incidents"]},
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the synthetic payments dataset.")
    parser.add_argument("--seed", type=int, default=GenerationConfig.seed)
    parser.add_argument("--days", type=int, default=GenerationConfig.days)
    parser.add_argument("--daily-volume", type=int, default=GenerationConfig.base_daily_volume)
    parser.add_argument("--out", type=Path, default=Path("data/generated"))
    args = parser.parse_args(argv)
    if args.days < 35:
        parser.error("--days must be at least 35 so both injected incidents fit in the window")

    config = GenerationConfig(seed=args.seed, days=args.days, base_daily_volume=args.daily_volume)
    data = generate(config)
    manifest = write_dataset(data, config, args.out)

    t = data.transactions
    print(f"Dataset {manifest['dataset_id']} written to {args.out}")
    print(f"  window        {manifest['window_start']}  ->  {manifest['window_end']}")
    print(f"  transactions  {len(t):,}  (failure rate {(t['status'] == 'FAILED').mean():.2%})")
    print(f"  incidents     {len(data.provider_incidents)}   deployments {len(data.deployments)}")
    print("  ground_truth.json is for evaluation only; it is not loaded into the database.")
    return 0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _normalised(weights: list[float]) -> np.ndarray:
    w = np.asarray(weights, dtype=float)
    return w / w.sum()


def _start(config: GenerationConfig) -> datetime:
    return datetime(config.start_date.year, config.start_date.month, config.start_date.day, tzinfo=timezone.utc)


def _iso(window_start: datetime, offset_seconds: float) -> str:
    return (window_start + timedelta(seconds=int(offset_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_since(window_start: datetime, iso: str) -> int:
    return int((datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - window_start).total_seconds())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
