import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

import pandas as pd

from engine import (DATA_SOURCES, cache_paths, load_data, monthly_breakdown, prepare_days,
                    simulate, summarize)
from strategy import Config

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--structure", default="strangle", choices=["strangle", "put", "call"])
    ap.add_argument("--delta", type=float, default=0.16)
    ap.add_argument("--stop", type=float, default=2.0)
    ap.add_argument("--tp", type=float, default=0.5)
    ap.add_argument("--iv-scale", type=float, default=1.0)
    ap.add_argument("--account", type=float, default=25000.0)
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--slippage", type=float, default=0.03)
    ap.add_argument("--commission", type=float, default=0.65)
    ap.add_argument("--vix-max", type=float, default=0.0)
    ap.add_argument("--vix-min", type=float, default=0.0)
    ap.add_argument("--skip-bwd", action="store_true")
    ap.add_argument("--trend-filter", action="store_true")
    ap.add_argument("--clock", default="trading", choices=["trading", "calendar"])
    ap.add_argument("--credit-haircut", type=float, default=1.0)
    ap.add_argument("--exit-iv-mult", type=float, default=1.0)
    ap.add_argument("--allow-missing-vix9d", action="store_true")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--run-name")
    args = ap.parse_args()

    cfg = Config(structure=args.structure, delta=args.delta, stop_mult=args.stop,
                 tp_mult=args.tp, iv_scale=args.iv_scale, account=args.account,
                 contracts=args.contracts, slippage=args.slippage,
                 commission=args.commission, vix_max=args.vix_max,
                 vix_min=args.vix_min, skip_bwd=args.skip_bwd,
                 trend_filter=args.trend_filter, clock=args.clock,
                 credit_haircut=args.credit_haircut,
                 exit_iv_mult=args.exit_iv_mult,
                 fail_closed_vix9d=not args.allow_missing_vix9d)

    print("SYNTHETIC OPTION-PRICE SCENARIO - NOT A HISTORICAL OPTIONS-CHAIN BACKTEST")
    print("Loading data...")
    daily, vix, vix9d, rth = load_data(refresh=args.refresh)
    days, session_audit = prepare_days(daily, vix, vix9d, rth, args.start, args.end,
                                       return_audit=True)
    excluded = session_audit[session_audit["status"] == "excluded"]
    evaluated_end = str(session_audit["date"].max()) if len(session_audit) else "none"
    print(f"complete trading days: {len(days)} | excluded sessions: {len(excluded)} "
          f"({args.start} -> {evaluated_end}; requested end {args.end})")
    if len(excluded):
        print("exclusions:", excluded["reason"].value_counts().to_dict())

    trades, curve = simulate(cfg, days)
    months = monthly_breakdown(trades, cfg)
    s = summarize(trades, curve, cfg)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = args.run_name or (
        f"{timestamp}_{cfg.structure}_d{int(cfg.delta * 100)}_stop{cfg.stop_mult:g}_"
        f"tp{cfg.tp_mult:g}_{cfg.clock}"
    )
    run_dir = os.path.join(RESULTS, "runs", safe_name)
    if os.path.exists(run_dir):
        raise FileExistsError(f"run directory already exists: {run_dir}")
    os.makedirs(run_dir, exist_ok=False)
    trades.to_csv(os.path.join(run_dir, "trades.csv"), index=False)
    months.to_csv(os.path.join(run_dir, "months.csv"), index=False)
    session_audit.to_csv(os.path.join(run_dir, "session_audit.csv"), index=False)

    def sha256(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    manifest = {
        "created_utc": timestamp,
        "warning": "Synthetic Black-Scholes/VIX scenario; no historical option quotes.",
        "config": vars(cfg),
        "period": {"requested_start": args.start, "requested_end": args.end,
                   "evaluated_schedule_end": evaluated_end,
                   "first_complete_day": str(days[0].date) if days else None,
                   "last_complete_day": str(days[-1].date) if days else None},
        "complete_days": len(days),
        "trades": len(trades),
        "data_sources": {
            name: {"source_url": data.attrs.get("source_url", DATA_SOURCES[name]),
                   "retrieved_utc": data.attrs.get("retrieved_utc")}
            for name, data in (("daily", daily), ("vix", vix), ("vix9d", vix9d),
                               ("rth", rth))
        },
        "input_cache_sha256": {name: sha256(path) for name, path in cache_paths().items()},
        "source_ranges": {
            "daily": [str(daily.index.min()), str(daily.index.max())],
            "vix": [str(vix.index.min()), str(vix.index.max())],
            "vix9d": [str(vix9d.index.min()), str(vix9d.index.max())],
            "hourly": [str(rth.index.min()), str(rth.index.max())],
        },
        "code_sha256": {
            name: sha256(os.path.join(os.path.dirname(__file__), name))
            for name in ("strategy.py", "engine.py", "backtest.py")
        },
        "summary": s,
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="ascii") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    pd.set_option("display.width", 220)
    print("\n=== Month by month ===")
    print(months.to_string(index=False))
    print("\n=== Overall ===")
    for k, v in s.items():
        print(f"{k}: {v}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(pd.to_datetime(curve["date"]), curve["equity"], lw=1.2)
        ax.set_title(f"SPY 0DTE {cfg.structure} {int(cfg.delta*100)}d "
                     f"stop {cfg.stop_mult}x tp {cfg.tp_mult or 'off'} - {args.start[:4]} equity (1 contract)")
        ax.set_ylabel("Equity ($)")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(run_dir, "equity_curve.png"), dpi=130)
        print(f"\nSaved immutable run artifacts to {run_dir}")
    except Exception as e:
        print("chart skipped:", e)


if __name__ == "__main__":
    main()
