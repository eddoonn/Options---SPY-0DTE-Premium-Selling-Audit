import argparse
import hashlib
import itertools
import json
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from engine import (DATA_SOURCES, cache_paths, load_data, monthly_breakdown, prepare_days,
                    simulate, summarize)
from strategy import Config

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, "results")

GRID = {
    "structure": ["strangle", "put", "call"],
    "delta": [0.10, 0.16, 0.20],
    "stop_mult": [1.5, 2.0, 3.0],
    "tp_mult": [0.0, 0.3, 0.5],
    "vix_max": [0.0, 20.0, 25.0],
    "skip_bwd": [False, True],
    "trend_filter": [False, True],
}

# These assumptions are fixed for the entire grid rather than optimized.
FIXED = {
    "account": 25000.0,
    "contracts": 1,
    "slippage": 0.03,
    "commission": 0.65,
    "iv_scale": 1.0,
    "clock": "trading",
    "credit_haircut": 1.0,
    "exit_iv_mult": 1.0,
    "fail_closed_vix9d": True,
}

DEVELOPMENT = {
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
}
REUSED_2026 = ("2026-01-01", "2026-12-31")
MIN_TRADES_PER_YEAR = 190
MIN_PF = 1.30
MAX_DD = 3500.0


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def json_ready(value):
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def risk_ratio(summary):
    return summary["pnl"] / max(abs(summary["maxdd"]), 1.0)


def main():
    parser = argparse.ArgumentParser(
        description="Development-only grid search; 2026 is already inspected and is not OOS."
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--evaluate-reused-2026", action="store_true")
    parser.add_argument("--run-name")
    args = parser.parse_args()

    print("DEVELOPMENT-ONLY SYNTHETIC GRID - NO UNTOUCHED HOLDOUT CLAIM")
    print("Selection uses 2024-2025 only. Any 2026 output is a reused diagnostic.")
    started = time.time()
    daily, vix, vix9d, rth = load_data(refresh=args.refresh)

    periods = dict(DEVELOPMENT)
    if args.evaluate_reused_2026:
        periods["reused_2026"] = REUSED_2026
    prepared = {}
    audits = {}
    for label, date_range in periods.items():
        prepared[label], audits[label] = prepare_days(
            daily, vix, vix9d, rth, *date_range, return_audit=True
        )
        excluded = int((audits[label]["status"] == "excluded").sum())
        print(f"{label}: {len(prepared[label])} complete sessions, {excluded} excluded")

    keys = list(GRID)
    combinations = itertools.product(*GRID.values())
    rows = []
    for index, values in enumerate(combinations, start=1):
        variable = dict(zip(keys, values))
        cfg = Config(**FIXED, **variable)
        trades24, curve24 = simulate(cfg, prepared["2024"])
        trades25, curve25 = simulate(cfg, prepared["2025"])
        y24 = summarize(trades24, curve24, cfg)
        y25 = summarize(trades25, curve25, cfg)
        eligible = (
            y24["trades"] >= MIN_TRADES_PER_YEAR
            and y25["trades"] >= MIN_TRADES_PER_YEAR
            and y24["pnl"] > 0
            and y25["pnl"] > 0
            and y24["pf"] >= MIN_PF
            and y25["pf"] >= MIN_PF
            and y24["maxdd"] >= -MAX_DD
            and y25["maxdd"] >= -MAX_DD
        )
        rows.append({
            **variable,
            "trades24": y24["trades"],
            "pnl24": y24["pnl"],
            "pf24": y24["pf"],
            "maxdd24": y24["maxdd"],
            "trades25": y25["trades"],
            "pnl25": y25["pnl"],
            "pf25": y25["pf"],
            "maxdd25": y25["maxdd"],
            "worst_year_pnl": min(y24["pnl"], y25["pnl"]),
            "total_pnl": y24["pnl"] + y25["pnl"],
            "development_score": min(risk_ratio(y24), risk_ratio(y25)),
            "eligible": eligible,
        })
        if index % 100 == 0:
            print(f"grid {index}/972 ({time.time() - started:.0f}s)")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_name = args.run_name or f"{timestamp}_development_only"
    run_dir = os.path.join(RESULTS, "optimization_runs", run_name)
    if os.path.exists(run_dir):
        raise FileExistsError(f"run directory already exists: {run_dir}")
    os.makedirs(run_dir, exist_ok=False)

    grid = pd.DataFrame(rows)
    grid.to_csv(os.path.join(run_dir, "development_grid.csv"), index=False)
    for label, audit in audits.items():
        audit.to_csv(os.path.join(run_dir, f"session_audit_{label}.csv"), index=False)

    eligible = grid[grid["eligible"]].sort_values(
        ["development_score", "worst_year_pnl", "total_pnl"],
        ascending=[False, False, False],
        kind="stable",
    )
    print(f"eligible development configs: {len(eligible)}/{len(grid)}")

    manifest = {
        "created_utc": timestamp,
        "warning": "Synthetic prices only; no historical option quotes and no untouched holdout.",
        "selection_data": "2024-2025 development data",
        "reused_2026_status": "Previously inspected; diagnostic only; never used for ranking.",
        "grid": GRID,
        "fixed_assumptions": FIXED,
        "constraints": {
            "minimum_trades_per_year": MIN_TRADES_PER_YEAR,
            "minimum_profit_factor_each_year": MIN_PF,
            "maximum_drawdown_dollars_each_year": MAX_DD,
            "positive_pnl_required_each_year": True,
        },
        "selection_rule": (
            "Highest minimum annual pnl/max-drawdown ratio, then highest worst-year PnL, "
            "then highest total PnL. No fallback if constraints fail."
        ),
        "eligible_configs": len(eligible),
        "data_sources": {
            name: {"source_url": data.attrs.get("source_url", DATA_SOURCES[name]),
                   "retrieved_utc": data.attrs.get("retrieved_utc")}
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
                        for name in ("strategy.py", "engine.py", "optimize.py")},
    }

    if eligible.empty:
        manifest["selection_status"] = "failed_no_eligible_configuration"
        print("Optimization failed: no configuration met the predeclared development constraints.")
    else:
        selected_row = eligible.iloc[0]
        selected_variables = {key: selected_row[key] for key in keys}
        for key in ("delta", "stop_mult", "tp_mult", "vix_max"):
            selected_variables[key] = float(selected_variables[key])
        for key in ("skip_bwd", "trend_filter"):
            selected_variables[key] = bool(selected_variables[key])
        selected_cfg = Config(**FIXED, **selected_variables)
        selection = {
            "status": "locked_from_2024_2025_development_only",
            "config": vars(selected_cfg),
            "development_metrics": {
                key: selected_row[key]
                for key in ("trades24", "pnl24", "pf24", "maxdd24", "trades25",
                            "pnl25", "pf25", "maxdd25", "worst_year_pnl", "total_pnl",
                            "development_score")
            },
            "code_sha256": manifest["code_sha256"],
            "input_cache_sha256": manifest["input_cache_sha256"],
        }
        with open(os.path.join(run_dir, "selected_config.json"), "w", encoding="ascii") as handle:
            json.dump(json_ready(selection), handle, indent=2, sort_keys=True, allow_nan=False)
        manifest["selection_status"] = selection["status"]
        manifest["selected_config"] = vars(selected_cfg)
        manifest["development_metrics"] = selection["development_metrics"]
        print("selected from development only:", selected_variables)

        if args.evaluate_reused_2026:
            trades, curve = simulate(selected_cfg, prepared["reused_2026"])
            summary = summarize(trades, curve, selected_cfg)
            trades.to_csv(os.path.join(run_dir, "reused_2026_trades.csv"), index=False)
            curve.to_csv(os.path.join(run_dir, "reused_2026_curve.csv"), index=False)
            monthly_breakdown(trades, selected_cfg).to_csv(
                os.path.join(run_dir, "reused_2026_months.csv"), index=False
            )
            manifest["reused_2026_diagnostic"] = summary
            print("reused 2026 diagnostic (not OOS):", summary)

    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="ascii") as handle:
        json.dump(json_ready(manifest), handle, indent=2, sort_keys=True, allow_nan=False)
    print(f"saved immutable optimization audit to {run_dir}")


if __name__ == "__main__":
    main()
