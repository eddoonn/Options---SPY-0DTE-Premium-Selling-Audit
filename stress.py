import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

import pandas as pd

from engine import DATA_SOURCES, cache_paths, load_data, prepare_days, simulate, summarize
from strategy import Config

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, "results")
DEVELOPMENT = {
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
}
REUSED_2026 = ("2026-01-01", "2026-12-31")

VARIANTS = {
    "baseline": {},
    "calendar_clock": {"clock": "calendar"},
    "slippage_0.10": {"slippage": 0.10},
    "slippage_0.20": {"slippage": 0.20},
    "commission_1.30": {"commission": 1.30},
    "credit_haircut_0.85": {"credit_haircut": 0.85},
    "exit_iv_1.25": {"exit_iv_mult": 1.25},
    "combined_adverse": {"credit_haircut": 0.85, "exit_iv_mult": 1.50,
                         "slippage": 0.10},
}


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def load_config(path):
    if not path:
        return Config(), None
    with open(path, "r", encoding="ascii") as handle:
        payload = json.load(handle)
    raw = payload.get("config", payload)
    allowed = Config.__dataclass_fields__
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"unknown config fields: {unknown}")
    return Config(**raw), sha256(path)


def main():
    parser = argparse.ArgumentParser(
        description="Sensitivity analysis of one locked config; never selects a winner."
    )
    parser.add_argument("--config", help="selected_config.json from optimize.py")
    parser.add_argument("--include-reused-2026", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--run-name")
    args = parser.parse_args()

    base, config_hash = load_config(args.config)
    daily, vix, vix9d, rth = load_data(refresh=args.refresh)
    periods = dict(DEVELOPMENT)
    if args.include_reused_2026:
        periods["reused_2026"] = REUSED_2026

    prepared = {}
    audits = {}
    for label, date_range in periods.items():
        prepared[label], audits[label] = prepare_days(
            daily, vix, vix9d, rth, *date_range, return_audit=True
        )

    rows = []
    for variant, overrides in VARIANTS.items():
        values = {**vars(base), **overrides}
        cfg = Config(**values)
        for period, days in prepared.items():
            trades, curve = simulate(cfg, days)
            rows.append({"variant": variant, "period": period, **summarize(trades, curve, cfg)})
    table = pd.DataFrame(rows)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_name = args.run_name or f"{timestamp}_locked_config_stress"
    run_dir = os.path.join(RESULTS, "stress_runs", run_name)
    if os.path.exists(run_dir):
        raise FileExistsError(f"run directory already exists: {run_dir}")
    os.makedirs(run_dir, exist_ok=False)
    table.to_csv(os.path.join(run_dir, "stress_results.csv"), index=False)
    for label, audit in audits.items():
        audit.to_csv(os.path.join(run_dir, f"session_audit_{label}.csv"), index=False)

    manifest = {
        "created_utc": timestamp,
        "warning": "Synthetic prices only. Stress results are sensitivity checks, not validation.",
        "config": vars(base),
        "config_path": os.path.abspath(args.config) if args.config else None,
        "config_file_sha256": config_hash,
        "period_labels": {
            "2024": "development",
            "2025": "development",
            **({"reused_2026": "previously inspected diagnostic"}
               if args.include_reused_2026 else {}),
        },
        "variants": VARIANTS,
        "data_sources": {
            name: data.attrs.get("source_url", DATA_SOURCES[name])
            for name, data in (("daily", daily), ("vix", vix), ("vix9d", vix9d),
                               ("rth", rth))
        },
        "source_ranges": {
            "daily": [str(daily.index.min()), str(daily.index.max())],
            "vix": [str(vix.index.min()), str(vix.index.max())],
            "vix9d": [str(vix9d.index.min()), str(vix9d.index.max())],
            "hourly": [str(rth.index.min()), str(rth.index.max())],
        },
        "input_cache_sha256": {name: sha256(path) for name, path in cache_paths().items()},
        "code_sha256": {name: sha256(os.path.join(ROOT, name))
                        for name in ("strategy.py", "engine.py", "stress.py")},
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="ascii") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    pd.set_option("display.width", 220)
    print("LOCKED-CONFIG SYNTHETIC SENSITIVITY - NO OOS CLAIM")
    print(table.to_string(index=False))
    print(f"saved immutable stress audit to {run_dir}")


if __name__ == "__main__":
    main()
